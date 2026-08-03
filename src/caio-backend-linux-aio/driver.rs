//! `caio_core::Driver` implementation over raw Linux AIO (`abi.rs`). No
//! PyO3: the bridge crate (`src/linux_aio`) owns the GIL and Python object
//! lifetimes; this crate only knows how to actually run a request.
//!
//! Kernel-visible `aio_data` is a numeric `RequestId` here, not a Python
//! object pointer - `inflight` is the registry that lets a completion's
//! `event.data` be turned back into the right buffer/iocb.

use std::collections::{HashMap, VecDeque};

use caio_core::{
    CompletionResult, Driver, DispatchError, DriverEvent, OpCode, OsError, PrepareError, RequestId, RequestSpec,
};

use crate::abi::{self, AioContextT, Iocb, IOCB_CMD_FDSYNC, IOCB_CMD_FSYNC, IOCB_CMD_PREAD, IOCB_CMD_PWRITE};

fn opcode_to_iocb_cmd(opcode: OpCode) -> u16 {
    match opcode {
        OpCode::Read => IOCB_CMD_PREAD,
        OpCode::Write => IOCB_CMD_PWRITE,
        OpCode::Fsync => IOCB_CMD_FSYNC,
        OpCode::Fdsync => IOCB_CMD_FDSYNC,
    }
}

enum KernelBuffer {
    Read(Box<[u8]>),
    // Never read back out (a write completion only reports a transferred
    // count, not the bytes) - held purely to keep `aio_buf`'s target alive
    // for as long as the kernel might still be reading from it.
    Write(#[allow(dead_code)] Box<[u8]>),
    None,
}

/// Everything that must stay alive, at a stable heap address, for as long
/// as the kernel might still touch it: the iocb itself (its raw address is
/// what `io_submit`/`io_cancel` pass to the kernel) and the buffer `aio_buf`
/// points into. Heap-allocated as a whole (`Box<InflightRequest>`) so
/// moving the *map entry* around (e.g. `HashMap` rehashing) never moves
/// this - only the `Box` pointer itself moves, never its target.
pub struct InflightRequest {
    iocb: Iocb,
    buffer: KernelBuffer,
}

impl InflightRequest {
    fn from_spec(spec: RequestSpec) -> Result<Self, PrepareError> {
        let opcode = spec.opcode();
        let mut iocb = Iocb::zeroed();
        iocb.aio_lio_opcode = opcode_to_iocb_cmd(opcode);

        let buffer = match spec {
            RequestSpec::Read { fd, offset, nbytes, priority } => {
                // Fallible: `nbytes` is entirely caller-controlled (up to
                // `MAX_TRANSFER_SIZE`, just under 2 GiB) - an infallible
                // `vec![0u8; nbytes]` would abort the whole process on OOM
                // instead of surfacing a catchable error.
                let mut buf = Vec::new();
                buf.try_reserve_exact(nbytes as usize)
                    .map_err(|e| PrepareError(format!("allocating {nbytes}-byte read buffer: {e}")))?;
                buf.resize(nbytes as usize, 0);
                let mut buf = buf.into_boxed_slice();
                iocb.aio_fildes = fd as u32;
                iocb.aio_offset = offset as i64;
                iocb.aio_reqprio = priority as i16;
                iocb.aio_buf = buf.as_mut_ptr() as u64;
                iocb.aio_nbytes = buf.len() as u64;
                KernelBuffer::Read(buf)
            }
            RequestSpec::Write { fd, offset, payload, priority } => {
                let mut buf = payload.into_boxed_slice();
                iocb.aio_fildes = fd as u32;
                iocb.aio_offset = offset as i64;
                iocb.aio_reqprio = priority as i16;
                iocb.aio_buf = buf.as_mut_ptr() as u64;
                iocb.aio_nbytes = buf.len() as u64;
                KernelBuffer::Write(buf)
            }
            RequestSpec::Fsync { fd, priority } | RequestSpec::Fdsync { fd, priority } => {
                iocb.aio_fildes = fd as u32;
                iocb.aio_reqprio = priority as i16;
                KernelBuffer::None
            }
        };

        Ok(InflightRequest { iocb, buffer })
    }

    /// Turns a completed (or cancelled) `res` into the typed result the
    /// engine expects.
    fn resolve(self, res: i64) -> CompletionResult {
        if res < 0 {
            return CompletionResult::Error(OsError::from_raw((-res) as i32));
        }
        match self.buffer {
            KernelBuffer::Read(buf) => {
                let transferred = (res as usize).min(buf.len());
                // `into_vec()` reuses the same allocation (no copy);
                // truncating down to the actual transferred count means a
                // short read doesn't hand back its untouched zeroed tail.
                let mut buf = buf.into_vec();
                buf.truncate(transferred);
                CompletionResult::Read { buffer: buf.into_boxed_slice(), transferred }
            }
            KernelBuffer::Write(_) => CompletionResult::Write { transferred: res as usize },
            KernelBuffer::None => CompletionResult::Sync,
        }
    }
}

pub struct LinuxAioDriver {
    ctx: AioContextT,
    eventfd: i32,
    inflight: HashMap<RequestId, Box<InflightRequest>>,
    pending_events: VecDeque<DriverEvent>,
}

impl LinuxAioDriver {
    pub fn new(max_requests: u32) -> std::io::Result<Self> {
        // EFD_NONBLOCK so poll()'s read() returns EAGAIN instead of
        // blocking when nothing is pending yet; EFD_CLOEXEC so this fd
        // doesn't leak into child processes spawned while alive.
        let eventfd = unsafe { libc::eventfd(0, libc::EFD_NONBLOCK | libc::EFD_CLOEXEC) };
        if eventfd < 0 {
            return Err(std::io::Error::last_os_error());
        }
        let ctx = abi::io_setup(max_requests)
            .inspect_err(|_| unsafe {
                libc::close(eventfd);
            })?;
        Ok(LinuxAioDriver { ctx, eventfd, inflight: HashMap::new(), pending_events: VecDeque::new() })
    }

    pub fn eventfd(&self) -> i32 {
        self.eventfd
    }

    /// `libc::read()` on the registered eventfd - `Ok(None)` means nothing
    /// pending yet (EAGAIN, since it's non-blocking), matching the raw
    /// low-level API's own `poll()` semantics.
    pub fn read_eventfd(&self) -> std::io::Result<Option<u64>> {
        let mut result: u64 = 0;
        let size = unsafe {
            libc::read(self.eventfd, &mut result as *mut u64 as *mut libc::c_void, std::mem::size_of::<u64>())
        };
        if size == std::mem::size_of::<u64>() as isize {
            Ok(Some(result))
        } else {
            let err = std::io::Error::last_os_error();
            if err.kind() == std::io::ErrorKind::WouldBlock {
                Ok(None)
            } else {
                Err(err)
            }
        }
    }

    /// Blocking wait for completions - the bridge calls this from inside
    /// `py.detach()` to release the GIL; this crate has no notion of the
    /// GIL itself.
    pub fn wait_for_events(
        &mut self, min_nr: usize, max_nr: usize, timeout: Option<libc::timespec>,
    ) -> std::io::Result<usize> {
        let events = abi::io_getevents(self.ctx, min_nr, max_nr, timeout)?;
        let count = events.len();
        for event in events {
            self.resolve_event(event);
        }
        Ok(count)
    }

    fn resolve_event(&mut self, event: abi::IoEvent) {
        let id = RequestId::new(event.data);
        let Some(inflight) = self.inflight.remove(&id) else {
            // A stray/duplicate event for an ID we don't (or no longer)
            // track is a kernel/driver invariant violation - treat it as a
            // loud panic rather than a silent no-op.
            panic!("io_getevents() reported a completion for unknown/already-reaped request {id:?}");
        };
        self.pending_events.push_back(DriverEvent::Completed(id, inflight.resolve(event.res)));
    }
}

impl Driver for LinuxAioDriver {
    type Submission = Box<InflightRequest>;

    fn supports_inflight_cancel(&self) -> bool {
        true
    }

    fn prepare(&mut self, spec: RequestSpec) -> Result<Self::Submission, PrepareError> {
        Ok(Box::new(InflightRequest::from_spec(spec)?))
    }

    fn dispatch(&mut self, id: RequestId, mut submission: Self::Submission) -> Result<bool, DispatchError> {
        submission.iocb.aio_data = id.as_u64();
        submission.iocb.aio_flags |= abi::IOCB_FLAG_RESFD;
        submission.iocb.aio_resfd = self.eventfd as u32;

        // Insert first so the iocb lives at its final, stable heap address
        // before we hand the kernel a pointer to it.
        self.inflight.insert(id, submission);
        let iocb_ptr = &mut self.inflight.get_mut(&id).expect("just inserted").iocb as *mut Iocb;

        // SAFETY: `iocb_ptr` points into the `Box` just inserted into
        // `inflight`, which owns it at a stable address until removed on
        // completion/cancellation - `io_submit`'s own safety contract.
        let outcome = unsafe { abi::io_submit(self.ctx, &[iocb_ptr]) };
        match outcome {
            Ok(1) => Ok(true), // genuinely dispatched
            Ok(_) | Err(_) => {
                // io_submit() rejected this one outright - queue-full
                // EAGAIN, or a hard per-request error (EBADF/EINVAL/
                // EFAULT). `DispatchError` surfaces this as a synchronous
                // Python exception instead of silently resolving it as an
                // async completion.
                let errno = match &outcome {
                    Err(e) => e.raw_os_error().unwrap_or(-1),
                    Ok(_) => libc::EAGAIN,
                };
                let _ = self.inflight.remove(&id).expect("just inserted");
                Err(DispatchError(OsError::from_raw(errno)))
            }
        }
    }

    fn cancel_inflight(&mut self, id: RequestId) {
        let Some(inflight) = self.inflight.get(&id) else { return };
        let iocb_ptr = &inflight.iocb as *const Iocb as *mut Iocb;
        // SAFETY: `iocb_ptr` is still a live, in-flight request registered
        // with this exact `ctx` (see `io_cancel`'s own safety contract).
        if let Ok(event) = unsafe { abi::io_cancel(self.ctx, iocb_ptr) } {
            // io_cancel() delivers the outcome synchronously as its own
            // return value, not through a later io_getevents() call.
            let inflight = self.inflight.remove(&id).expect("just checked it's present");
            self.pending_events.push_back(DriverEvent::Completed(id, inflight.resolve(event.res)));
        }
        // On failure (EAGAIN "already running/finished", ENOSYS, ...) this
        // is a pure no-op: the request stays exactly as it was, and its
        // real completion (if any) will still arrive normally later.
    }

    fn cancel_queued(&mut self, _id: RequestId) {
        unreachable!("dispatch() always reports SUBMITTED - nothing is ever left QUEUED to cancel");
    }

    fn poll(&mut self) -> Vec<DriverEvent> {
        // Drain whatever's already queued (immediate dispatch/cancel
        // resolutions) first, then peek the kernel non-blockingly for
        // anything new - bounded by the current outstanding count, which
        // is itself already bounded by this Context's own max_requests
        // (validated at construction), so no separate cap is needed here.
        let max_nr = self.inflight.len();
        if max_nr > 0 {
            let zero_timeout = libc::timespec { tv_sec: 0, tv_nsec: 0 };
            if let Ok(events) = abi::io_getevents(self.ctx, 0, max_nr, Some(zero_timeout)) {
                for event in events {
                    self.resolve_event(event);
                }
            }
        }
        self.pending_events.drain(..).collect()
    }

    fn shutdown(&mut self) -> Vec<RequestId> {
        // Outstanding requests are not reaped here - the bridge's GC
        // `__traverse__`/`__clear__` impls can reach this point with a
        // request still genuinely in flight. Safe regardless: `io_destroy()`
        // blocks until any outstanding iocbs actually finish.
        let leaked: Vec<RequestId> = self.inflight.keys().copied().collect();
        if self.ctx != 0 {
            let _ = abi::io_destroy(self.ctx);
            self.ctx = 0;
        }
        if self.eventfd >= 0 {
            unsafe { libc::close(self.eventfd) };
            self.eventfd = -1;
        }
        leaked
    }
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use std::io::Write;
    use std::os::unix::io::AsRawFd;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::Mutex;
    use std::time::{Duration, Instant};

    use caio_core::{CancelStatus, ContextId, Engine, RejectReason};

    static NEXT_TEST_FILE: AtomicU64 = AtomicU64::new(0);

    /// Serializes every test in this module: `dispatch_rejects_write_to_closed_fd_synchronously`
    /// deliberately closes a real fd and relies on that exact number
    /// staying invalid, which a concurrently-running test's own
    /// `temp_file()` call could otherwise reopen and reuse first.
    static TEST_SERIAL: Mutex<()> = Mutex::new(());

    fn temp_file() -> std::fs::File {
        let unique = NEXT_TEST_FILE.fetch_add(1, Ordering::Relaxed);
        let path = std::env::temp_dir().join(format!("caio-backend-linux-aio-test-{}-{unique}", std::process::id()));
        std::fs::OpenOptions::new().create(true).truncate(true).read(true).write(true).open(path).unwrap()
    }

    fn poll_until<F: FnMut() -> bool>(mut done: F, timeout: Duration) {
        let deadline = Instant::now() + timeout;
        while !done() {
            assert!(Instant::now() < deadline, "timed out waiting for completion");
            std::thread::sleep(Duration::from_millis(1));
        }
    }

    fn make_engine(max_requests: u32) -> Engine<LinuxAioDriver> {
        Engine::new(ContextId::new(1), max_requests as usize, LinuxAioDriver::new(max_requests).unwrap())
    }

    #[test]
    fn write_then_read_round_trip() {
        let _guard = TEST_SERIAL.lock().unwrap();
        let file = temp_file();
        let fd = file.as_raw_fd();
        let mut engine = make_engine(4);

        let write_id =
            engine.submit_one(RequestSpec::write(fd, 0, b"hello world".to_vec().into_boxed_slice(), 0).unwrap()).unwrap();
        let mut completions = Vec::new();
        poll_until(|| { completions.extend(engine.poll()); !completions.is_empty() }, Duration::from_secs(5));
        match &completions[0].1 {
            CompletionResult::Write { transferred } => assert_eq!(*transferred, 11),
            other => panic!("expected Write completion, got {other:?}"),
        }
        assert_eq!(write_id.request_id(), completions[0].0);

        let read_id = engine.submit_one(RequestSpec::read(fd, 0, 11, 0).unwrap()).unwrap();
        let mut completions = Vec::new();
        poll_until(|| { completions.extend(engine.poll()); !completions.is_empty() }, Duration::from_secs(5));
        match &completions[0].1 {
            CompletionResult::Read { buffer, transferred } => {
                assert_eq!(*transferred, 11);
                assert_eq!(&buffer[..], b"hello world");
            }
            other => panic!("expected Read completion, got {other:?}"),
        }
        assert_eq!(read_id.request_id(), completions[0].0);
    }

    #[test]
    fn short_read_truncates_buffer_to_transferred_count() {
        let _guard = TEST_SERIAL.lock().unwrap();
        let mut file = temp_file();
        file.write_all(b"AB").unwrap();
        let fd = file.as_raw_fd();
        let mut engine = make_engine(4);

        let read_id = engine.submit_one(RequestSpec::read(fd, 0, 256, 0).unwrap()).unwrap();
        let mut completions = Vec::new();
        poll_until(|| { completions.extend(engine.poll()); !completions.is_empty() }, Duration::from_secs(5));
        assert_eq!(completions[0].0, read_id.request_id());
        match &completions[0].1 {
            CompletionResult::Read { buffer, transferred } => {
                assert_eq!(*transferred, 2);
                assert_eq!(buffer.len(), 2, "linux_aio truncates the buffer itself to the transferred count");
                assert_eq!(&buffer[..], b"AB");
            }
            other => panic!("expected Read completion, got {other:?}"),
        }
    }

    #[test]
    fn zero_length_read_and_write_complete_cleanly() {
        let _guard = TEST_SERIAL.lock().unwrap();
        let file = temp_file();
        let fd = file.as_raw_fd();
        let mut engine = make_engine(4);

        let write_id = engine.submit_one(RequestSpec::write(fd, 0, Vec::new().into_boxed_slice(), 0).unwrap()).unwrap();
        let read_id = engine.submit_one(RequestSpec::read(fd, 0, 0, 0).unwrap()).unwrap();

        let mut completions = Vec::new();
        poll_until(|| { completions.extend(engine.poll()); completions.len() >= 2 }, Duration::from_secs(5));
        for (id, result) in &completions {
            if *id == write_id.request_id() {
                assert!(matches!(result, CompletionResult::Write { transferred: 0 }), "expected zero-length Write, got {result:?}");
            } else if *id == read_id.request_id() {
                match result {
                    CompletionResult::Read { buffer, transferred } => {
                        assert_eq!(*transferred, 0);
                        assert!(buffer.is_empty());
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
        let _guard = TEST_SERIAL.lock().unwrap();
        let file = temp_file();
        let fd = file.as_raw_fd();
        let mut engine = make_engine(2);

        let fsync_id = engine.submit_one(RequestSpec::fsync(fd, 0)).unwrap();
        let fdsync_id = engine.submit_one(RequestSpec::fdsync(fd, 0)).unwrap();

        let mut completions = Vec::new();
        poll_until(|| { completions.extend(engine.poll()); completions.len() >= 2 }, Duration::from_secs(5));
        for (id, result) in &completions {
            assert!(matches!(result, CompletionResult::Sync), "expected Sync, got {result:?}");
            assert!(*id == fsync_id.request_id() || *id == fdsync_id.request_id());
        }
    }

    #[test]
    fn dispatch_rejects_write_to_closed_fd_synchronously() {
        // io_submit() validates the fd eagerly and fails synchronously
        // (EBADF) rather than ever posting an async completion for it.
        let _guard = TEST_SERIAL.lock().unwrap();
        let closed_fd = {
            let file = temp_file();
            file.as_raw_fd()
        }; // `file` dropped here - fd is now closed.
        let mut engine = make_engine(4);

        let rejected = engine
            .submit_one(RequestSpec::write(closed_fd, 0, b"x".to_vec().into_boxed_slice(), 0).unwrap())
            .expect_err("a closed fd must be rejected synchronously, not accepted");
        assert!(
            matches!(rejected.reason, RejectReason::DispatchFailed(_)),
            "expected DispatchFailed, got {:?}",
            rejected.reason,
        );
        assert_eq!(engine.outstanding_count(), 0, "a synchronously rejected request must not linger in the registry");
    }

    #[test]
    fn cancel_inflight_is_requested_and_still_completes() {
        // io_cancel() races against real completion - either outcome
        // (genuinely cancelled, or completed first) is valid; what must
        // hold is that cancel() itself reports `Requested` (not silently
        // ignored) and exactly one terminal completion eventually arrives.
        let _guard = TEST_SERIAL.lock().unwrap();
        let file = temp_file();
        let fd = file.as_raw_fd();
        let mut engine = make_engine(4);
        let id = engine.submit_one(RequestSpec::write(fd, 0, b"x".to_vec().into_boxed_slice(), 0).unwrap()).unwrap();

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
    #[should_panic(expected = "unknown/already-reaped")]
    fn resolve_event_panics_on_unknown_id() {
        let mut driver = LinuxAioDriver::new(4).unwrap();
        driver.resolve_event(abi::IoEvent { data: 999, obj: 0, res: 0, res2: 0 });
    }

    #[test]
    fn shutdown_reports_leaked_ids_for_outstanding_requests() {
        let _guard = TEST_SERIAL.lock().unwrap();
        let file = temp_file();
        let fd = file.as_raw_fd();
        let mut engine = make_engine(4);
        let id = engine.submit_one(RequestSpec::write(fd, 0, b"x".to_vec().into_boxed_slice(), 0).unwrap()).unwrap();

        // Deliberately never drained via poll() - still outstanding when
        // shutdown() runs.
        let leaked = engine.shutdown();
        assert_eq!(leaked, vec![id.request_id()]);
    }
}
