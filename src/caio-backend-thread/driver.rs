//! `caio_core::Driver` implementation backed by a bounded thread pool
//! (`pool.rs`) doing portable blocking I/O (`platform_io.rs`). No PyO3: the
//! bridge crate (`src/thread_aio`) owns callbacks, the GIL, and Python
//! object lifetimes; this crate only knows how to actually run a request.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use caio_core::{
    CompletionResult, Driver, DriverEvent, DispatchError, OpCode, OsError, PrepareError, RequestId, RequestSpec,
};

use crate::platform_io::{self, RawFdT};
use crate::pool::{Pool, Task};

/// Everything a worker thread needs to run the blocking syscall - no
/// dependency on `RequestSpec` or any Python-adjacent type past this point.
/// Public only because it's `ThreadDriver`'s associated `Submission` type;
/// its fields stay private, nothing outside this module constructs or
/// inspects one directly.
pub struct Job {
    opcode: OpCode,
    fd: RawFdT,
    offset: u64,
    buf: Vec<u8>,
}

impl Job {
    /// The only fallible step: allocating a zeroed read buffer (size is
    /// entirely caller-controlled) or copying the write payload out of the
    /// `RequestSpec` this consumes (see `Driver::prepare`'s own doc for why
    /// consuming rather than borrowing avoids a second copy here).
    fn from_spec(spec: RequestSpec) -> Result<Self, PrepareError> {
        match spec {
            RequestSpec::Read { fd, offset, nbytes, .. } => {
                let mut buf = Vec::new();
                buf.try_reserve_exact(nbytes as usize)
                    .map_err(|e| PrepareError(format!("allocating {nbytes}-byte read buffer: {e}")))?;
                buf.resize(nbytes as usize, 0);
                Ok(Job { opcode: OpCode::Read, fd, offset, buf })
            }
            RequestSpec::Write { fd, offset, payload, .. } => {
                // Zero-copy: `payload` is already a fully-materialized,
                // owned allocation by this point (copied once, at the PyO3
                // bridge layer, from the caller's Python `bytes`) - a
                // further `.as_slice().to_vec()` here would make a second,
                // wholly redundant copy of it.
                Ok(Job { opcode: OpCode::Write, fd, offset, buf: payload.into_boxed_slice().into_vec() })
            }
            RequestSpec::Fsync { fd, .. } => Ok(Job { opcode: OpCode::Fsync, fd, offset: 0, buf: Vec::new() }),
            RequestSpec::Fdsync { fd, .. } => Ok(Job { opcode: OpCode::Fdsync, fd, offset: 0, buf: Vec::new() }),
        }
    }

    fn execute(mut self) -> CompletionResult {
        let syscall: std::io::Result<usize> = match self.opcode {
            OpCode::Read => platform_io::pread(self.fd, &mut self.buf, self.offset),
            OpCode::Write => platform_io::pwrite(self.fd, &self.buf, self.offset),
            OpCode::Fsync => platform_io::fsync(self.fd).map(|_| 0usize),
            OpCode::Fdsync => platform_io::fdatasync(self.fd).map(|_| 0usize),
        };

        // A well-behaved syscall never reports transferring more bytes than
        // the buffer it was given - on Windows, a race around a closed fd
        // can make ReadFile report success with a nonsensical byte count
        // instead of a clean error, so a bogus larger `n` must not be
        // trusted (it would make the truncation below a no-op).
        let syscall = syscall.and_then(|n| {
            if matches!(self.opcode, OpCode::Read | OpCode::Write) && n > self.buf.len() {
                Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    format!(
                        "syscall reported {n} bytes transferred but the buffer is only {} bytes; refusing to trust this result",
                        self.buf.len(),
                    ),
                ))
            } else {
                Ok(n)
            }
        });

        match syscall {
            Ok(n) => match self.opcode {
                OpCode::Read => {
                    self.buf.truncate(n);
                    CompletionResult::Read { buffer: self.buf.into_boxed_slice(), transferred: n }
                }
                OpCode::Write => CompletionResult::Write { transferred: n },
                OpCode::Fsync | OpCode::Fdsync => CompletionResult::Sync,
            },
            Err(e) => {
                // On Windows, GetLastError() can legitimately read back as
                // 0 (ERROR_SUCCESS) for a call that still failed (a race
                // around a closed fd) - treat that the same as "no OS code
                // available" instead of reporting a fabricated success.
                let errno = match e.raw_os_error() {
                    Some(0) | None => -1,
                    Some(code) => code,
                };
                CompletionResult::Error(OsError::from_raw(errno))
            }
        }
    }
}

/// `Driver` backed by a bounded worker-thread pool. `dispatch()` always
/// reports the request as immediately `SUBMITTED`: this backend has no
/// mechanism to cancel a job once it's handed to the pool, whether it's
/// still queued or a worker has already started running it
/// (`supports_inflight_cancel()` is `false`), so `Engine` never needs to
/// distinguish those two.
pub struct ThreadDriver {
    pool: Pool,
    events: Arc<Mutex<VecDeque<DriverEvent>>>,
    /// Called on the worker thread, immediately after a completion is
    /// pushed into `events` - lets a bridge crate (which knows about the
    /// GIL, unlike this one) reacquire it and drain/deliver completions
    /// right there, with zero added latency and no separate polling step.
    /// A no-op closure is a valid choice for callers (e.g. tests) that
    /// drive `Engine::poll()` themselves instead.
    on_event: Arc<dyn Fn() + Send + Sync>,
}

impl ThreadDriver {
    pub fn new(pool_size: usize, queue_size: usize, on_event: Arc<dyn Fn() + Send + Sync>) -> Self {
        ThreadDriver { pool: Pool::new(pool_size, queue_size), events: Arc::new(Mutex::new(VecDeque::new())), on_event }
    }
}

impl Driver for ThreadDriver {
    type Submission = Job;

    fn supports_inflight_cancel(&self) -> bool {
        false
    }

    fn prepare(&mut self, spec: RequestSpec) -> Result<Self::Submission, PrepareError> {
        Job::from_spec(spec)
    }

    fn dispatch(&mut self, id: RequestId, job: Self::Submission) -> Result<bool, DispatchError> {
        let events = Arc::clone(&self.events);
        let on_event = Arc::clone(&self.on_event);
        let task = Task {
            run: Box::new(move || {
                let result = job.execute();
                events.lock().unwrap().push_back(DriverEvent::Completed(id, result));
                on_event();
            }),
        };
        // `Engine`'s own capacity guard never calls `dispatch()` beyond the
        // capacity this pool was constructed with, so this can't actually
        // be full. Never fails for any other reason either - unlike
        // linux_aio, nothing here validates the fd/params eagerly.
        self.pool.submit(task).expect("Engine's own capacity guard must prevent a full pool queue here");
        Ok(true)
    }

    fn cancel_inflight(&mut self, _id: RequestId) {
        unreachable!("supports_inflight_cancel() is false - Engine never calls this");
    }

    fn cancel_queued(&mut self, _id: RequestId) {
        unreachable!("dispatch() always reports SUBMITTED - nothing is ever left QUEUED to cancel");
    }

    fn poll(&mut self) -> Vec<DriverEvent> {
        self.events.lock().unwrap().drain(..).collect()
    }

    fn shutdown(&mut self) -> Vec<RequestId> {
        self.pool.shutdown_and_join();
        Vec::new()
    }
}

impl ThreadDriver {
    /// See `Pool::signal_shutdown`/`take_workers`: a PyO3 bridge whose
    /// `Engine` lives behind a lock a worker's `on_event` callback also
    /// needs (to drain/deliver its own completion before it can exit)
    /// must not hold that lock across a blocking join - it would deadlock
    /// against exactly that worker. Call `signal_shutdown()`, drop your
    /// lock, then join the handles from `take_workers()` outside it,
    /// instead of calling the plain `Driver::shutdown()` trait method.
    pub fn signal_shutdown(&self) {
        self.pool.signal_shutdown();
    }

    pub fn take_workers(&mut self) -> Vec<std::thread::JoinHandle<()>> {
        self.pool.take_workers()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{Duration, Instant};

    use caio_core::{ContextId, Engine};

    static NEXT_TEST_FILE: AtomicU64 = AtomicU64::new(0);

    /// Serializes every test in this module against every other one -
    /// acquired as the first statement of each `#[test]` fn below, held
    /// for the test's whole body. Needed because fd numbers are
    /// process-global: `write_to_closed_fd_reports_an_os_error_not_a_panic`
    /// closes a real fd and relies on that number staying invalid, but
    /// `cargo test`'s parallelism means another test's `temp_file()` could
    /// reopen and reuse it first, turning the expected EBADF into a
    /// successful write against a stranger's file.
    static TEST_SERIAL: Mutex<()> = Mutex::new(());

    /// Cross-platform RAII wrapper around a fresh temp file, exposing its
    /// raw CRT-fd number via `libc::open`/`libc::close` on both Unix and
    /// Windows - deliberately not `std::fs::File`, which wraps a raw
    /// HANDLE on Windows rather than a CRT fd (`platform_io.rs`'s own
    /// `with_file()` converts the other way, via `_get_osfhandle`). Lets
    /// every test below run identically on both platforms.
    struct TestFile(RawFdT);

    impl TestFile {
        fn new() -> Self {
            let unique = NEXT_TEST_FILE.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir().join(format!("caio-backend-thread-test-{}-{unique}", std::process::id()));
            let c_path = std::ffi::CString::new(path.to_str().unwrap()).unwrap();
            // O_BINARY only exists (and only matters) on Windows - without
            // it the CRT would translate bytes on read/write (e.g. LF <->
            // CRLF), corrupting exact-byte-count assertions below.
            #[cfg(windows)]
            let platform_flags = libc::O_BINARY;
            #[cfg(not(windows))]
            let platform_flags = 0;
            let fd = unsafe {
                libc::open(c_path.as_ptr(), libc::O_CREAT | libc::O_TRUNC | libc::O_RDWR | platform_flags, 0o600)
            };
            assert!(fd >= 0, "failed to open {}: {}", path.display(), std::io::Error::last_os_error());
            TestFile(fd)
        }

        fn fd(&self) -> RawFdT {
            self.0
        }
    }

    impl Drop for TestFile {
        fn drop(&mut self) {
            unsafe {
                libc::close(self.0);
            }
        }
    }

    /// A counter (not just thread ID) keeps each test's file unique: two
    /// different tests can land on the same thread ID and would otherwise
    /// silently share a file.
    fn temp_file() -> TestFile {
        TestFile::new()
    }

    fn poll_until<F: FnMut() -> bool>(mut done: F, timeout: Duration) {
        let deadline = Instant::now() + timeout;
        while !done() {
            assert!(Instant::now() < deadline, "timed out waiting for completion");
            std::thread::sleep(Duration::from_millis(1));
        }
    }

    fn make_engine(pool_size: usize, queue_size: usize) -> Engine<ThreadDriver> {
        Engine::new(ContextId::new(1), queue_size, ThreadDriver::new(pool_size, queue_size, Arc::new(|| {})))
    }

    #[test]
    fn on_event_fires_from_the_worker_thread_without_polling() {
        let _guard = TEST_SERIAL.lock().unwrap();
        let file = temp_file();
        let fd = file.fd();
        let woken = Arc::new(Mutex::new(false));
        let woken_writer = Arc::clone(&woken);
        let driver = ThreadDriver::new(
            1,
            4,
            Arc::new(move || {
                *woken_writer.lock().unwrap() = true;
            }),
        );
        let mut engine = Engine::new(ContextId::new(1), 4, driver);

        engine.submit_one(RequestSpec::write(fd, 0, b"x".to_vec().into_boxed_slice(), 0).unwrap()).unwrap();
        poll_until(|| *woken.lock().unwrap(), Duration::from_secs(5));
    }

    #[test]
    fn write_then_read_round_trip() {
        let _guard = TEST_SERIAL.lock().unwrap();
        let file = temp_file();
        let fd = file.fd();
        let mut engine = make_engine(4, 8);

        let write_spec = RequestSpec::write(fd, 0, b"hello world".to_vec().into_boxed_slice(), 0).unwrap();
        let write_id = engine.submit_one(write_spec).unwrap();

        let mut completions = Vec::new();
        poll_until(
            || {
                completions.extend(engine.poll());
                !completions.is_empty()
            },
            Duration::from_secs(5),
        );
        match &completions[0].1 {
            CompletionResult::Write { transferred } => assert_eq!(*transferred, 11),
            other => panic!("expected Write completion, got {other:?}"),
        }
        assert_eq!(write_id.request_id(), completions[0].0);

        let read_spec = RequestSpec::read(fd, 0, 11, 0).unwrap();
        let read_id = engine.submit_one(read_spec).unwrap();
        let mut completions = Vec::new();
        poll_until(
            || {
                completions.extend(engine.poll());
                !completions.is_empty()
            },
            Duration::from_secs(5),
        );
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
    fn fsync_and_fdsync_complete_with_sync_result() {
        let _guard = TEST_SERIAL.lock().unwrap();
        let file = temp_file();
        let fd = file.fd();
        let mut engine = make_engine(2, 4);

        let fsync_id = engine.submit_one(RequestSpec::fsync(fd, 0)).unwrap();
        let fdsync_id = engine.submit_one(RequestSpec::fdsync(fd, 0)).unwrap();

        let mut completions = Vec::new();
        poll_until(
            || {
                completions.extend(engine.poll());
                completions.len() >= 2
            },
            Duration::from_secs(5),
        );
        for (id, result) in &completions {
            assert!(matches!(result, CompletionResult::Sync), "expected Sync, got {result:?}");
            assert!(*id == fsync_id.request_id() || *id == fdsync_id.request_id());
        }
    }

    #[test]
    fn read_past_end_of_file_returns_short_read() {
        let _guard = TEST_SERIAL.lock().unwrap();
        let file = temp_file();
        let fd = file.fd();
        let mut engine = make_engine(2, 4);

        // Empty file - a read of any size transfers zero bytes, not an error.
        let read_id = engine.submit_one(RequestSpec::read(fd, 0, 64, 0).unwrap()).unwrap();
        let mut completions = Vec::new();
        poll_until(
            || {
                completions.extend(engine.poll());
                !completions.is_empty()
            },
            Duration::from_secs(5),
        );
        assert_eq!(completions[0].0, read_id.request_id());
        match &completions[0].1 {
            CompletionResult::Read { buffer, transferred } => {
                assert_eq!(*transferred, 0);
                assert!(buffer.is_empty());
            }
            other => panic!("expected a short Read, got {other:?}"),
        }
    }

    #[test]
    fn write_to_closed_fd_reports_an_os_error_not_a_panic() {
        let _guard = TEST_SERIAL.lock().unwrap();
        let closed_fd = {
            let file = temp_file();
            file.fd()
        }; // `file` dropped here - fd is now closed.
        let mut engine = make_engine(2, 4);

        let id = engine
            .submit_one(RequestSpec::write(closed_fd, 0, b"x".to_vec().into_boxed_slice(), 0).unwrap())
            .unwrap();
        let mut completions = Vec::new();
        poll_until(
            || {
                completions.extend(engine.poll());
                !completions.is_empty()
            },
            Duration::from_secs(5),
        );
        assert_eq!(completions[0].0, id.request_id());
        assert!(completions[0].1.is_error(), "expected an error completion for a closed fd");
    }

    #[test]
    fn capacity_is_enforced_by_the_engine_not_just_the_pool() {
        let _guard = TEST_SERIAL.lock().unwrap();
        let file = temp_file();
        let fd = file.fd();
        // Single worker, small queue - deliberately easy to overflow.
        let mut engine = make_engine(1, 2);

        let specs: Vec<_> = (0..5).map(|_| RequestSpec::write(fd, 0, b"x".to_vec().into_boxed_slice(), 0).unwrap()).collect();
        let report = engine.submit_many(specs);
        assert!(report.accepted.len() <= 2, "must never accept more than capacity");
        assert!(!report.rejected.is_empty());

        poll_until(
            || {
                let _ = engine.poll();
                engine.outstanding_count() == 0
            },
            Duration::from_secs(5),
        );
    }

    #[test]
    fn cancel_on_a_thread_backed_request_is_always_unsupported() {
        let _guard = TEST_SERIAL.lock().unwrap();
        let file = temp_file();
        let fd = file.fd();
        let mut engine = make_engine(1, 4);

        let id = engine.submit_one(RequestSpec::write(fd, 0, b"x".to_vec().into_boxed_slice(), 0).unwrap()).unwrap();
        let status = engine.cancel(id).unwrap();
        assert_eq!(status, caio_core::CancelStatus::Unsupported);

        poll_until(
            || {
                let _ = engine.poll();
                engine.outstanding_count() == 0
            },
            Duration::from_secs(5),
        );
    }

    #[test]
    fn oversized_read_is_a_clean_prepare_error_not_a_crash() {
        // RequestSpec::read() itself already rejects this size (caio-core's
        // own MAX_TRANSFER_SIZE check) - nothing thread-backend-specific to
        // exercise here beyond confirming this backend doesn't need its own
        // separate size guard on top of that.
        let huge = caio_core::MAX_TRANSFER_SIZE + 1;
        assert!(RequestSpec::read(3, 0, huge, 0).is_err());
    }

    #[test]
    fn shutdown_joins_workers_without_hanging() {
        let _guard = TEST_SERIAL.lock().unwrap();
        let file = temp_file();
        let fd = file.fd();
        let mut engine = make_engine(4, 8);
        for _ in 0..4 {
            engine.submit_one(RequestSpec::write(fd, 0, b"x".to_vec().into_boxed_slice(), 0).unwrap()).unwrap();
        }
        poll_until(
            || {
                let _ = engine.poll();
                engine.outstanding_count() == 0
            },
            Duration::from_secs(5),
        );
        engine.shutdown();
    }
}
