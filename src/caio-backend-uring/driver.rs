//! `caio_core::Driver` implementation over raw io_uring (`abi.rs`). No
//! PyO3: the bridge crate (`src/linux_uring`) owns the GIL and Python
//! object lifetimes; this crate only knows how to manage the rings and
//! actually run a request.
//!
//! Kernel-visible `user_data` is a numeric `RequestId` here, not a Python
//! object pointer - `inflight` is the registry that turns a completion's
//! `cqe.user_data` back into the right buffer/opcode. Buffers are plain
//! Rust-owned `Box<[u8]>` - an accepted extra copy vs. a zero-copy pointer
//! straight into a `bytes` object, in exchange for one ownership model
//! shared by all three backends.

use std::collections::HashMap;
use std::io;
use std::sync::atomic::{AtomicU32, Ordering};

use caio_core::{
    CompletionResult, Driver, DispatchError, DriverEvent, OpCode, OsError, PrepareError, RequestId, RequestSpec,
};

use crate::abi::{self, IoUringCqe, IoUringParams, IoUringSqe};

/// Sentinel `user_data` for the SQE an ASYNC_CANCEL request itself uses -
/// never a real `RequestId` (see `IdSequence`'s own doc comment: reaching
/// `u64::MAX` through ordinary submission is not reachable on any real
/// workload), so `poll()` knows to skip its CQE rather than treat it as a
/// real completion.
const CANCEL_USER_DATA: u64 = u64::MAX;

struct MmapRegion {
    ptr: *mut libc::c_void,
    len: usize,
}

// SAFETY: this is exactly the kind of shared memory mmap(MAP_SHARED) is
// designed for - the kernel and this process synchronize access to it
// purely through the atomic head/tail operations used everywhere this
// pointer is dereferenced.
unsafe impl Send for MmapRegion {}
unsafe impl Sync for MmapRegion {}

impl MmapRegion {
    fn map(len: usize, fd: i32, offset: i64) -> io::Result<Self> {
        let ptr = unsafe {
            libc::mmap(
                std::ptr::null_mut(),
                len,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_SHARED | libc::MAP_POPULATE,
                fd,
                offset,
            )
        };
        if ptr == libc::MAP_FAILED {
            return Err(io::Error::last_os_error());
        }
        Ok(MmapRegion { ptr, len })
    }

    unsafe fn atomic_u32(&self, byte_offset: u32) -> &AtomicU32 {
        &*(self.ptr.add(byte_offset as usize) as *const AtomicU32)
    }

    unsafe fn plain_u32(&self, byte_offset: u32) -> u32 {
        *(self.ptr.add(byte_offset as usize) as *const u32)
    }

    unsafe fn as_mut_ptr<T>(&self, byte_offset: u32) -> *mut T {
        self.ptr.add(byte_offset as usize) as *mut T
    }
}

impl Drop for MmapRegion {
    fn drop(&mut self) {
        // Self-contained unmap means `UringDriver::new`'s early-return
        // error paths (e.g. the CQ mapping failing after the SQ mapping
        // already succeeded) can't leak a successful mapping just because
        // nothing downstream ever got a chance to close it explicitly -
        // dropping the local variable that holds it is enough on its own.
        unsafe {
            libc::munmap(self.ptr, self.len);
        }
    }
}

struct Inflight {
    opcode: OpCode,
    /// `Some` for Read (to fill in and hand back) and Write (kept alive
    /// only - the kernel reads from it, nothing reads it back out on this
    /// side); `None` for Fsync/Fdsync.
    buffer: Option<Box<[u8]>>,
}

impl Inflight {
    fn resolve(self, res: i32) -> CompletionResult {
        if res < 0 {
            return CompletionResult::Error(OsError::from_raw(-res));
        }
        match (self.opcode, self.buffer) {
            (OpCode::Read, Some(buf)) => {
                // Unlike thread_aio/linux_aio, the buffer handed back here
                // is the *full*, originally-allocated (zero-initialized)
                // size, not truncated to `transferred` - linux_uring's own
                // `.payload` getter is documented to expose the untouched,
                // zero-filled tail past a short read, while `.get_value()`/
                // `nbytes` use `transferred` to report just the valid
                // portion. Truncating here would destroy that tail before
                // the bridge ever sees it.
                let transferred = (res as usize).min(buf.len());
                CompletionResult::Read { buffer: buf, transferred }
            }
            (OpCode::Write, _) => CompletionResult::Write { transferred: res as usize },
            _ => CompletionResult::Sync,
        }
    }
}

/// A `prepare()`d request, not yet handed to the ring.
pub struct UringSubmission {
    opcode: OpCode,
    fd: i32,
    offset: u64,
    len: u32,
    buffer: Option<Box<[u8]>>,
}

impl UringSubmission {
    fn from_spec(spec: RequestSpec) -> Result<Self, PrepareError> {
        match spec {
            RequestSpec::Read { fd, offset, nbytes, .. } => {
                let mut buf = Vec::new();
                buf.try_reserve_exact(nbytes as usize)
                    .map_err(|e| PrepareError(format!("allocating {nbytes}-byte read buffer: {e}")))?;
                buf.resize(nbytes as usize, 0);
                Ok(UringSubmission {
                    opcode: OpCode::Read,
                    fd,
                    offset,
                    len: nbytes as u32,
                    buffer: Some(buf.into_boxed_slice()),
                })
            }
            RequestSpec::Write { fd, offset, payload, .. } => {
                let buf = payload.into_boxed_slice();
                Ok(UringSubmission { opcode: OpCode::Write, fd, offset, len: buf.len() as u32, buffer: Some(buf) })
            }
            RequestSpec::Fsync { fd, .. } => {
                Ok(UringSubmission { opcode: OpCode::Fsync, fd, offset: 0, len: 0, buffer: None })
            }
            RequestSpec::Fdsync { fd, .. } => {
                Ok(UringSubmission { opcode: OpCode::Fdsync, fd, offset: 0, len: 0, buffer: None })
            }
        }
    }

    fn addr(&self) -> u64 {
        match &self.buffer {
            Some(buf) => buf.as_ptr() as u64,
            None => 0,
        }
    }
}

fn sqe_opcode(opcode: OpCode) -> u8 {
    match opcode {
        OpCode::Read => abi::IORING_OP_READ,
        OpCode::Write => abi::IORING_OP_WRITE,
        OpCode::Fsync | OpCode::Fdsync => abi::IORING_OP_FSYNC,
    }
}

fn sqe_op_flags(opcode: OpCode) -> u32 {
    if opcode == OpCode::Fdsync { abi::IORING_FSYNC_DATASYNC } else { 0 }
}

pub struct UringDriver {
    uring_fd: i32,
    eventfd_fd: i32,
    sqpoll: bool,
    no_sqarray: bool,

    sq_ring: MmapRegion,
    /// `None` when the CQ ring shares the SQ ring's mapping
    /// (`IORING_FEAT_SINGLE_MMAP`) - munmap'd through `sq_ring` instead.
    cq_ring: Option<MmapRegion>,
    sqes: MmapRegion,

    sq_off: abi::IoSqringOffsets,
    cq_off: abi::IoCqringOffsets,
    sq_ring_mask: u32,
    sq_ring_entries: u32,
    cq_ring_mask: u32,

    inflight: HashMap<RequestId, Inflight>,
}

impl UringDriver {
    fn cq_region(&self) -> &MmapRegion {
        self.cq_ring.as_ref().unwrap_or(&self.sq_ring)
    }

    unsafe fn sq_head(&self) -> &AtomicU32 {
        self.sq_ring.atomic_u32(self.sq_off.head)
    }
    unsafe fn sq_tail(&self) -> &AtomicU32 {
        self.sq_ring.atomic_u32(self.sq_off.tail)
    }
    unsafe fn sq_flags(&self) -> &AtomicU32 {
        self.sq_ring.atomic_u32(self.sq_off.flags)
    }
    unsafe fn sq_array(&self) -> *mut u32 {
        self.sq_ring.as_mut_ptr(self.sq_off.array)
    }
    unsafe fn cq_head(&self) -> &AtomicU32 {
        self.cq_region().atomic_u32(self.cq_off.head)
    }
    unsafe fn cq_tail(&self) -> &AtomicU32 {
        self.cq_region().atomic_u32(self.cq_off.tail)
    }
    unsafe fn cqes(&self) -> *const IoUringCqe {
        self.cq_region().as_mut_ptr(self.cq_off.cqes)
    }
    unsafe fn sqes(&self) -> *mut IoUringSqe {
        self.sqes.ptr as *mut IoUringSqe
    }

    pub fn eventfd(&self) -> i32 {
        self.eventfd_fd
    }

    pub fn read_eventfd(&self) -> io::Result<Option<u64>> {
        let mut result: u64 = 0;
        let size = unsafe {
            libc::read(self.eventfd_fd, &mut result as *mut u64 as *mut libc::c_void, std::mem::size_of::<u64>())
        };
        if size == std::mem::size_of::<u64>() as isize {
            Ok(Some(result))
        } else {
            let err = io::Error::last_os_error();
            if err.kind() == io::ErrorKind::WouldBlock { Ok(None) } else { Err(err) }
        }
    }

    /// Non-blocking: how many CQEs are currently sitting in the ring,
    /// without any syscall - used by the bridge's own deadline-bounded
    /// waits (`process_events(timeout=...)`, and `Drop`'s bounded reap).
    pub fn cq_available(&self) -> u32 {
        unsafe {
            let head = self.cq_head().load(Ordering::Relaxed);
            let tail = self.cq_tail().load(Ordering::Acquire);
            tail.wrapping_sub(head)
        }
    }

    /// Non-blocking pump: asks the kernel to make progress and post any
    /// completions it already has ready (SQPOLL wakeup, or inline-
    /// completed page-cache ops) without waiting for more. Used by the
    /// bridge between `cq_available()` checks in its own poll-with-
    /// deadline loops - a raw `io_uring_enter(GETEVENTS)` has no timeout
    /// of its own, so the bridge, not this call, owns bounding the wait.
    pub fn pump_getevents(&self) -> io::Result<()> {
        unsafe { abi::io_uring_enter(self.uring_fd, 0, 0, abi::IORING_ENTER_GETEVENTS) }.map(|_| ())
    }

    /// Submits all currently-staged SQEs to the kernel in one
    /// `io_uring_enter` call (or wakes the SQPOLL thread). Corresponds to
    /// the raw low-level API's own `flush()` - separate from `dispatch()`
    /// itself, which only stages an SQE locally (see `dispatch`'s own doc
    /// comment for why this split exists).
    pub fn flush(&self) -> io::Result<()> {
        if self.sqpoll {
            let sq_flags = unsafe { self.sq_flags().load(Ordering::Relaxed) };
            if sq_flags & abi::IORING_SQ_NEED_WAKEUP != 0 {
                unsafe { abi::io_uring_enter(self.uring_fd, 0, 0, abi::IORING_ENTER_SQ_WAKEUP) }?;
            }
            return Ok(());
        }

        let (tail, head) =
            unsafe { (self.sq_tail().load(Ordering::Relaxed), self.sq_head().load(Ordering::Acquire)) };
        let mut remaining = tail.wrapping_sub(head);
        while remaining > 0 {
            let submitted = unsafe { abi::io_uring_enter(self.uring_fd, remaining, 0, 0) }?;
            remaining -= submitted;
        }
        Ok(())
    }

    /// Probes `io_uring_setup` with progressively less demanding flag
    /// combinations, then sets up the mmap'd SQ/CQ rings and registers the
    /// completion eventfd. `IORING_SETUP_SINGLE_ISSUER` is deliberately
    /// never tried (see its own constant doc in `abi.rs`).
    pub fn new(max_requests: u32, want_sqpoll: bool) -> io::Result<Self> {
        // EFD_NONBLOCK so poll()'s read() returns EAGAIN instead of
        // blocking when nothing is pending yet; EFD_CLOEXEC so this fd
        // doesn't leak into child processes spawned while alive.
        let eventfd_fd = unsafe { libc::eventfd(0, libc::EFD_NONBLOCK | libc::EFD_CLOEXEC) };
        if eventfd_fd < 0 {
            return Err(io::Error::last_os_error());
        }
        Self::setup(max_requests, want_sqpoll, eventfd_fd).inspect_err(|_| unsafe {
            libc::close(eventfd_fd);
        })
    }

    fn setup(max_requests: u32, want_sqpoll: bool, eventfd_fd: i32) -> io::Result<Self> {
        let flag_table: &[u32] = if want_sqpoll {
            &[abi::IORING_SETUP_SQPOLL | abi::IORING_SETUP_NO_SQARRAY, abi::IORING_SETUP_SQPOLL, 0]
        } else {
            &[abi::IORING_SETUP_NO_SQARRAY, 0]
        };

        let mut uring_fd = -1;
        let mut params = IoUringParams::zeroed();
        let mut flags_used = 0u32;

        for &flags in flag_table {
            let mut attempt = IoUringParams::zeroed();
            attempt.flags = flags;
            if flags & abi::IORING_SETUP_SQPOLL != 0 {
                attempt.sq_thread_idle = 100;
            }
            match abi::io_uring_setup(max_requests, &mut attempt) {
                Ok(fd) => {
                    uring_fd = fd;
                    params = attempt;
                    flags_used = flags;
                    break;
                }
                Err(e) => {
                    let errno = e.raw_os_error().unwrap_or(-1);
                    if errno != libc::EINVAL && errno != libc::EPERM {
                        return Err(e);
                    }
                }
            }
        }
        if uring_fd < 0 {
            return Err(io::Error::other("io_uring_setup failed for every fallback flag combination"));
        }

        let no_sqarray = flags_used & abi::IORING_SETUP_NO_SQARRAY != 0;
        let sqpoll = flags_used & abi::IORING_SETUP_SQPOLL != 0;

        let sq_ring_size = if no_sqarray {
            (params.sq_off.flags as usize) + std::mem::size_of::<u32>()
        } else {
            (params.sq_off.array as usize) + (params.sq_entries as usize) * std::mem::size_of::<u32>()
        };
        let cq_ring_size =
            (params.cq_off.cqes as usize) + (params.cq_entries as usize) * std::mem::size_of::<IoUringCqe>();

        let close_and_err = |uring_fd: i32, e: io::Error| -> io::Error {
            unsafe { libc::close(uring_fd) };
            e
        };

        let single_mmap = params.features & abi::IORING_FEAT_SINGLE_MMAP != 0;

        let (sq_ring, cq_ring) = if single_mmap {
            let size = sq_ring_size.max(cq_ring_size);
            let region = MmapRegion::map(size, uring_fd, abi::IORING_OFF_SQ_RING)
                .map_err(|e| close_and_err(uring_fd, e))?;
            (region, None)
        } else {
            let sq = MmapRegion::map(sq_ring_size, uring_fd, abi::IORING_OFF_SQ_RING)
                .map_err(|e| close_and_err(uring_fd, e))?;
            let cq = MmapRegion::map(cq_ring_size, uring_fd, abi::IORING_OFF_CQ_RING)
                .map_err(|e| close_and_err(uring_fd, e))?;
            (sq, Some(cq))
        };

        let sqes_size = (params.sq_entries as usize) * std::mem::size_of::<IoUringSqe>();
        let sqes = MmapRegion::map(sqes_size, uring_fd, abi::IORING_OFF_SQES)
            .map_err(|e| close_and_err(uring_fd, e))?;

        let sq_ring_mask = unsafe { sq_ring.plain_u32(params.sq_off.ring_mask) };
        let sq_ring_entries = unsafe { sq_ring.plain_u32(params.sq_off.ring_entries) };
        let cq_region_ref = cq_ring.as_ref().unwrap_or(&sq_ring);
        let cq_ring_mask = unsafe { cq_region_ref.plain_u32(params.cq_off.ring_mask) };

        let evfd_reg_async = !sqpoll;
        abi::io_uring_register_eventfd(uring_fd, eventfd_fd, evfd_reg_async)
            .map_err(|e| close_and_err(uring_fd, e))?;

        Ok(UringDriver {
            uring_fd,
            eventfd_fd,
            sqpoll,
            no_sqarray,
            sq_ring,
            cq_ring,
            sqes,
            sq_off: params.sq_off,
            cq_off: params.cq_off,
            sq_ring_mask,
            sq_ring_entries,
            cq_ring_mask,
            inflight: HashMap::new(),
        })
    }
}

impl Driver for UringDriver {
    type Submission = UringSubmission;

    fn supports_inflight_cancel(&self) -> bool {
        true
    }

    fn prepare(&mut self, spec: RequestSpec) -> Result<Self::Submission, PrepareError> {
        UringSubmission::from_spec(spec)
    }

    /// Only *stages* the SQE locally (in the mmap'd SQ ring and `inflight`);
    /// does not itself call `io_uring_enter` - that's `flush()`. Always
    /// reports `Ok(true)` (immediately `SUBMITTED`) rather than `Ok(false)`
    /// (`QUEUED`): the ring slot this claims is what the engine's capacity
    /// accounting needs to track regardless of whether `flush()` has run
    /// yet, and nothing Python-visible distinguishes "staged" from "kernel
    /// has it" today.
    fn dispatch(&mut self, id: RequestId, submission: Self::Submission) -> Result<bool, DispatchError> {
        // SAFETY: ring pointers/masks come from a successfully initialized
        // io_uring instance and are valid for this driver's whole lifetime.
        unsafe {
            let tail = self.sq_tail().load(Ordering::Relaxed);
            let head = self.sq_head().load(Ordering::Acquire);
            // Engine's capacity guard doesn't know cancel_inflight() can
            // also stage its own ASYNC_CANCEL SQE, consuming a ring slot no
            // Engine-tracked request occupies - trusting Engine's count
            // alone could overflow the ring. Checking real ring occupancy
            // here (same check cancel_inflight makes below) closes that gap.
            if tail.wrapping_sub(head) >= self.sq_ring_entries {
                return Err(DispatchError(OsError::from_raw(libc::EAGAIN)));
            }
            let index = tail & self.sq_ring_mask;
            let sqe = &mut *self.sqes().add(index as usize);
            *sqe = IoUringSqe::zeroed();
            sqe.fd = submission.fd;
            sqe.off = submission.offset;
            sqe.user_data = id.as_u64();
            sqe.opcode = sqe_opcode(submission.opcode);
            sqe.addr = submission.addr();
            sqe.len = submission.len;
            sqe.op_flags = sqe_op_flags(submission.opcode);

            if !self.no_sqarray {
                *self.sq_array().add(index as usize) = index;
            }
            self.sq_tail().store(tail.wrapping_add(1), Ordering::Release);
        }

        self.inflight.insert(id, Inflight { opcode: submission.opcode, buffer: submission.buffer });
        Ok(true)
    }

    /// Fire-and-forget: the target's own completion (cancelled or not)
    /// always shows up through the normal `poll()` path, tagged with the
    /// same `RequestId` it was submitted with. `addr` carries that ID,
    /// since that's what the kernel matches an `ASYNC_CANCEL` target
    /// against.
    fn cancel_inflight(&mut self, id: RequestId) {
        unsafe {
            let tail = self.sq_tail().load(Ordering::Relaxed);
            let head = self.sq_head().load(Ordering::Acquire);
            if tail.wrapping_sub(head) >= self.sq_ring_entries {
                return; // best-effort: no room to even stage the cancel SQE
            }

            let index = tail & self.sq_ring_mask;
            let sqe = &mut *self.sqes().add(index as usize);
            *sqe = IoUringSqe::zeroed();
            sqe.opcode = abi::IORING_OP_ASYNC_CANCEL;
            sqe.addr = id.as_u64();
            sqe.user_data = CANCEL_USER_DATA;

            if !self.no_sqarray {
                *self.sq_array().add(index as usize) = index;
            }
            self.sq_tail().store(tail.wrapping_add(1), Ordering::Release);

            let _ = abi::io_uring_enter(self.uring_fd, 1, 0, 0);
        }
    }

    fn cancel_queued(&mut self, _id: RequestId) {
        unreachable!("dispatch() always reports SUBMITTED - nothing is ever left QUEUED to cancel");
    }

    /// Drains all currently-available CQEs from the completion ring - no
    /// syscall, reads directly from the mmap'd ring. Commits (advances
    /// `head`) before returning - a caller that reentrantly calls this
    /// again (e.g. from within a callback it invokes based on this call's
    /// results) must see the ring state this pass leaves behind, not the
    /// state from before it started, or it would walk the same CQE(s)
    /// again (double-free once both reconstructions eventually drop) -
    /// this is exactly why `Engine` itself never invokes callbacks, only
    /// the bridge does, strictly after this returns.
    fn poll(&mut self) -> Vec<DriverEvent> {
        let mut events = Vec::new();
        // `head` is tracked as a plain local `u32`, not a bound
        // `&AtomicU32`, deliberately: holding a reference derived from
        // `self` (via `self.cq_head()`) across the loop below would
        // conflict with `self.inflight.remove()`'s `&mut self` need in
        // the borrow checker's eyes, even though they touch disjoint
        // fields - a fresh `self.cq_head()` call at the very end (once
        // nothing else borrows `self`) avoids that entirely.
        //
        // SAFETY: ring pointers/masks come from a successfully
        // initialized io_uring instance and stay valid for this driver's
        // whole lifetime.
        let (mut head, tail) = unsafe {
            (self.cq_head().load(Ordering::Relaxed), self.cq_tail().load(Ordering::Acquire))
        };

        while head != tail {
            // SAFETY: see above.
            let cqe = unsafe { &*self.cqes().add((head & self.cq_ring_mask) as usize) };
            if cqe.user_data != CANCEL_USER_DATA {
                let id = RequestId::new(cqe.user_data);
                if let Some(inflight) = self.inflight.remove(&id) {
                    events.push(DriverEvent::Completed(id, inflight.resolve(cqe.res)));
                }
                // A missing registry entry means a stale/duplicate CQE -
                // silently ignored rather than panicking, since there's no
                // way to distinguish that from a ring wraparound edge case
                // this pass doesn't attempt to rule out.
            }
            head = head.wrapping_add(1);
        }

        unsafe {
            self.cq_head().store(head, Ordering::Release);
        }
        events
    }

    fn shutdown(&mut self) -> Vec<RequestId> {
        // The bridge's own Drop does the deadline-bounded reap (flush +
        // poll-with-timeout, GIL released) *before* calling this - by the
        // time this runs, `inflight` should already be empty in normal
        // use. If it isn't (a genuinely wedged kernel/filesystem past the
        // bridge's own deadline), closing the fd and letting the ring
        // mmaps unmap via field drops anyway is a safe best-effort fallback.
        let leaked: Vec<RequestId> = self.inflight.keys().copied().collect();
        if self.uring_fd >= 0 {
            unsafe { libc::close(self.uring_fd) };
            self.uring_fd = -1;
        }
        if self.eventfd_fd >= 0 {
            unsafe { libc::close(self.eventfd_fd) };
            self.eventfd_fd = -1;
        }
        leaked
    }
}

/// Re-exported so the bridge's `Drop` can size its own reap deadline the
/// same way this driver's own docs describe it.
pub const DROP_REAP_TIMEOUT_SECS: u64 = 30;

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use std::io::Write;
    use std::os::unix::io::AsRawFd;
    use std::sync::atomic::{AtomicU64, Ordering as StdOrdering};
    use std::time::{Duration, Instant};

    use caio_core::{CancelStatus, ContextId, Engine};

    static NEXT_TEST_FILE: AtomicU64 = AtomicU64::new(0);

    /// No SQPOLL here (`want_sqpoll: false`): SQPOLL needs a privileged/
    /// specially-configured kernel thread that may not be available in
    /// A counter (not just PID) keeps each test's file unique since
    /// `cargo test` runs tests in parallel.
    fn temp_file() -> std::fs::File {
        let unique = NEXT_TEST_FILE.fetch_add(1, StdOrdering::Relaxed);
        let path = std::env::temp_dir().join(format!("caio-backend-uring-test-{}-{unique}", std::process::id()));
        std::fs::OpenOptions::new().create(true).truncate(true).read(true).write(true).open(path).unwrap()
    }

    fn poll_until<F: FnMut() -> bool>(mut done: F, timeout: Duration) {
        let deadline = Instant::now() + timeout;
        while !done() {
            assert!(Instant::now() < deadline, "timed out waiting for completion");
            std::thread::sleep(Duration::from_millis(1));
        }
    }

    // No SQPOLL (`want_sqpoll: false`): it needs a privileged/specially-
    // configured kernel thread that may not be available in every CI/VM
    // environment, and none of these tests are about SQPOLL itself.
    fn make_engine(max_requests: u32) -> Engine<UringDriver> {
        Engine::new(ContextId::new(1), max_requests as usize, UringDriver::new(max_requests, false).unwrap())
    }

    /// `dispatch()` only *stages* an SQE - nothing reaches the kernel until
    /// `flush()` runs. Every test below must call this after
    /// `submit_one`/`submit_many` or nothing will ever complete.
    fn flush(engine: &mut Engine<UringDriver>) {
        engine.driver_mut().flush().unwrap();
    }

    #[test]
    fn new_and_shutdown_with_nothing_outstanding() {
        let mut engine = make_engine(4);
        assert!(engine.shutdown().is_empty());
    }

    #[test]
    fn write_then_read_round_trip() {
        let file = temp_file();
        let fd = file.as_raw_fd();
        let mut engine = make_engine(4);

        let write_id =
            engine.submit_one(RequestSpec::write(fd, 0, b"hello world".to_vec().into_boxed_slice(), 0).unwrap()).unwrap();
        flush(&mut engine);
        let mut completions = Vec::new();
        poll_until(|| { completions.extend(engine.poll()); !completions.is_empty() }, Duration::from_secs(5));
        match &completions[0].1 {
            CompletionResult::Write { transferred } => assert_eq!(*transferred, 11),
            other => panic!("expected Write completion, got {other:?}"),
        }
        assert_eq!(write_id.request_id(), completions[0].0);

        let read_id = engine.submit_one(RequestSpec::read(fd, 0, 11, 0).unwrap()).unwrap();
        flush(&mut engine);
        let mut completions = Vec::new();
        poll_until(|| { completions.extend(engine.poll()); !completions.is_empty() }, Duration::from_secs(5));
        match &completions[0].1 {
            CompletionResult::Read { buffer, transferred } => {
                assert_eq!(*transferred, 11);
                assert_eq!(&buffer[..11], b"hello world");
            }
            other => panic!("expected Read completion, got {other:?}"),
        }
        assert_eq!(read_id.request_id(), completions[0].0);
    }

    #[test]
    fn short_read_buffer_is_full_size_with_zero_tail() {
        // The one deliberate behavioral difference from thread_aio/
        // linux_aio: the buffer handed back is the FULL, originally-
        // requested size (zero-initialized tail intact past a short read),
        // not truncated to `transferred` - linux_uring's own `.payload`
        // getter depends on this.
        let mut file = temp_file();
        file.write_all(b"AB").unwrap();
        let fd = file.as_raw_fd();
        let mut engine = make_engine(4);

        let read_id = engine.submit_one(RequestSpec::read(fd, 0, 256, 0).unwrap()).unwrap();
        flush(&mut engine);
        let mut completions = Vec::new();
        poll_until(|| { completions.extend(engine.poll()); !completions.is_empty() }, Duration::from_secs(5));
        assert_eq!(completions[0].0, read_id.request_id());
        match &completions[0].1 {
            CompletionResult::Read { buffer, transferred } => {
                assert_eq!(*transferred, 2);
                assert_eq!(buffer.len(), 256, "the full, originally-requested buffer size must be preserved");
                assert_eq!(&buffer[..2], b"AB");
                assert!(buffer[2..].iter().all(|&b| b == 0), "untouched tail past a short read must be zero");
            }
            other => panic!("expected Read completion, got {other:?}"),
        }
    }

    #[test]
    fn zero_length_read_and_write_complete_cleanly() {
        let file = temp_file();
        let fd = file.as_raw_fd();
        let mut engine = make_engine(4);

        let write_id = engine.submit_one(RequestSpec::write(fd, 0, Vec::new().into_boxed_slice(), 0).unwrap()).unwrap();
        let read_id = engine.submit_one(RequestSpec::read(fd, 0, 0, 0).unwrap()).unwrap();
        flush(&mut engine);

        let mut completions = Vec::new();
        poll_until(|| { completions.extend(engine.poll()); completions.len() >= 2 }, Duration::from_secs(5));
        for (id, result) in &completions {
            if *id == write_id.request_id() {
                assert!(matches!(result, CompletionResult::Write { transferred: 0 }), "expected zero-length Write, got {result:?}");
            } else if *id == read_id.request_id() {
                match result {
                    CompletionResult::Read { buffer, transferred } => {
                        assert_eq!(*transferred, 0);
                        assert!(buffer.is_empty(), "a zero-length read requests (and gets) a zero-length buffer");
                    }
                    other => panic!("expected zero-length Read, got {other:?}"),
                }
            } else {
                panic!("unexpected id {id:?} in completions");
            }
        }
    }

    #[test]
    fn fsync_and_fdsync_complete_with_sync_result() {
        let file = temp_file();
        let fd = file.as_raw_fd();
        let mut engine = make_engine(2);

        let fsync_id = engine.submit_one(RequestSpec::fsync(fd, 0)).unwrap();
        let fdsync_id = engine.submit_one(RequestSpec::fdsync(fd, 0)).unwrap();
        flush(&mut engine);

        let mut completions = Vec::new();
        poll_until(|| { completions.extend(engine.poll()); completions.len() >= 2 }, Duration::from_secs(5));
        for (id, result) in &completions {
            assert!(matches!(result, CompletionResult::Sync), "expected Sync, got {result:?}");
            assert!(*id == fsync_id.request_id() || *id == fdsync_id.request_id());
        }
    }

    #[test]
    fn ring_wraparound_survives_many_sequential_writes() {
        // Deliberately small ring capacity (io_uring_setup rounds this up
        // to the next power of two internally) so ~50 sequential
        // submit/flush/poll rounds wrap the SQ/CQ ring indices many times
        // over - exercises the same modular index arithmetic
        // (`tail & ring_mask`) a long-running real workload would
        // eventually hit, without needing a multi-billion-operation test.
        let file = temp_file();
        let fd = file.as_raw_fd();
        let mut engine = make_engine(2);

        for i in 0..50u64 {
            let id = engine
                .submit_one(RequestSpec::write(fd, i * 8, b"deadbeef".to_vec().into_boxed_slice(), 0).unwrap())
                .unwrap();
            flush(&mut engine);
            let mut completions = Vec::new();
            poll_until(|| { completions.extend(engine.poll()); !completions.is_empty() }, Duration::from_secs(5));
            assert_eq!(completions.len(), 1);
            assert_eq!(completions[0].0, id.request_id());
            match &completions[0].1 {
                CompletionResult::Write { transferred } => assert_eq!(*transferred, 8),
                other => panic!("expected Write completion at iteration {i}, got {other:?}"),
            }
        }
    }

    #[test]
    fn cancel_inflight_is_requested_and_still_completes() {
        // ASYNC_CANCEL races against real completion - either outcome
        // (genuinely cancelled, or completed first) is valid; what must
        // hold is that cancel() itself reports `Requested` (not silently
        // ignored) and exactly one terminal completion eventually arrives.
        let file = temp_file();
        let fd = file.as_raw_fd();
        let mut engine = make_engine(4);
        let id = engine.submit_one(RequestSpec::write(fd, 0, b"x".to_vec().into_boxed_slice(), 0).unwrap()).unwrap();
        flush(&mut engine); // the original write must reach the kernel before cancelling it

        let status = engine.cancel(id).unwrap();
        assert!(matches!(status, CancelStatus::Requested), "expected Requested, got {status:?}");

        let mut completions = Vec::new();
        poll_until(|| { completions.extend(engine.poll()); engine.outstanding_count() == 0 }, Duration::from_secs(5));
        assert_eq!(completions.len(), 1);
        assert_eq!(completions[0].0, id.request_id());
    }

    #[test]
    fn poll_with_nothing_inflight_returns_empty() {
        let mut engine = make_engine(4);
        assert!(engine.poll().is_empty());
    }

    #[test]
    fn dispatch_rejects_when_sq_ring_is_actually_full() {
        // Engine's own capacity guard only counts outstanding requests it
        // knows about - it has no idea cancel_inflight() can also stage
        // its own ASYNC_CANCEL SQE, consuming a ring slot no
        // Engine-tracked request occupies. Simulates that mismatch
        // directly: advance sq_tail past what the (real, kernel-tracked)
        // sq_head has consumed, without going through dispatch() itself,
        // so the ring looks genuinely full from dispatch()'s own point of
        // view regardless of what any Engine capacity bookkeeping would
        // have allowed.
        let mut driver = UringDriver::new(2, false).unwrap();
        let entries = driver.sq_ring_entries;
        // SAFETY: `driver`'s ring mappings are valid for its whole
        // lifetime; storing to the mmap'd sq_tail is exactly what
        // `dispatch()`/`cancel_inflight()` themselves already do.
        unsafe {
            driver.sq_tail().store(entries, StdOrdering::Release);
        }

        let file = temp_file();
        let fd = file.as_raw_fd();
        let submission =
            UringSubmission::from_spec(RequestSpec::write(fd, 0, b"x".to_vec().into_boxed_slice(), 0).unwrap())
                .unwrap();
        let result = driver.dispatch(RequestId::new(999), submission);
        assert!(matches!(result, Err(DispatchError(_))), "expected a full-ring rejection, got {result:?}");
    }

    #[test]
    fn poll_silently_ignores_stale_cqe_for_unknown_id() {
        // Manufacture a CQE for an id this driver never dispatched by
        // writing directly into the mmap'd ring and advancing its tail -
        // exercises the same "stale/duplicate completion" path a real
        // wraparound race could hit, without needing to actually engineer
        // that race.
        let driver = UringDriver::new(4, false).unwrap();

        // SAFETY: `driver`'s ring mappings are valid for its whole
        // lifetime; writing a CQE and advancing `cq_tail` is exactly the
        // kernel's own side of the CQ ring protocol, done here by hand.
        unsafe {
            let tail = driver.cq_tail().load(StdOrdering::Relaxed);
            let index = tail & driver.cq_ring_mask;
            let cqe = &mut *(driver.cqes() as *mut IoUringCqe).add(index as usize);
            *cqe = IoUringCqe { user_data: 424242, res: 0, flags: 0 };
            driver.cq_tail().store(tail.wrapping_add(1), StdOrdering::Release);
        }

        let mut driver = driver;
        let events = driver.poll();
        assert!(events.is_empty(), "a stale/unknown CQE must be silently ignored, not surfaced or panicked on");
    }

    #[test]
    fn shutdown_reports_leaked_ids_for_outstanding_requests() {
        let file = temp_file();
        let fd = file.as_raw_fd();
        let mut engine = make_engine(4);
        let id = engine.submit_one(RequestSpec::write(fd, 0, b"x".to_vec().into_boxed_slice(), 0).unwrap()).unwrap();

        // Deliberately never flushed/drained - `dispatch()` already
        // recorded it in `inflight` regardless, so it's still outstanding
        // (from this driver's own point of view) when shutdown() runs.
        let leaked = engine.shutdown();
        assert_eq!(leaked, vec![id.request_id()]);
    }
}
