use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use pyo3::exceptions::{PyOverflowError, PySystemError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyTuple;
use pyo3_stub_gen::derive::{gen_stub_pyclass, gen_stub_pymethods};

use caio_core::{CompletionResult, ContextId, Engine, RejectReason, RequestId};
use caio_backend_uring::{UringDriver, DROP_REAP_TIMEOUT_SECS};

use crate::operation::AIOOperation;

const CTX_MAX_REQUESTS_DEFAULT: u32 = 32;
const EV_MAX_REQUESTS_DEFAULT: u32 = 512;

fn next_context_id() -> ContextId {
    static NEXT: AtomicU64 = AtomicU64::new(1);
    ContextId::new(NEXT.fetch_add(1, Ordering::Relaxed))
}

/// Applies whatever completions are currently available and invokes their
/// callbacks. Commits state (removing each completed request from the
/// registry, via `engine.poll()`) *before* invoking any callback, and -
/// just as importantly - is called with no lock on `engine`/`registry`
/// still held: a callback that reentrantly calls back into
/// `submit()`/`flush()`/`process_events()` on this same Context must be
/// able to reacquire those locks, not deadlock against itself (see
/// `AIOContext`'s own doc comment on why `engine`/`registry` are
/// `Mutex`-wrapped fields with `&self` methods at all).
fn deliver(py: Python<'_>, registry: &Mutex<HashMap<RequestId, Py<AIOOperation>>>, completions: Vec<(RequestId, CompletionResult)>) {
    if completions.is_empty() {
        return;
    }
    let mut reg = registry.lock().unwrap();
    let ops: Vec<_> = completions.into_iter().filter_map(|(id, result)| reg.remove(&id).map(|op| (op, result))).collect();
    drop(reg);

    for (op_py, result) in ops {
        let op_bound = op_py.bind(py);
        let op_ref = op_bound.borrow();
        op_ref.apply_result(result);
        let callback = op_ref.take_callback();
        let result_value = op_ref.result_value();
        drop(op_ref);

        if let Some(cb) = callback {
            if let Err(err) = cb.call1(py, (result_value,)) {
                err.write_unraisable(py, None);
            }
        }
    }
}

/// io_uring AIO context.
///
/// `engine`/`registry` are `Mutex`-wrapped, and every method below takes
/// `&self` (never `&mut self`), even though nothing here is actually
/// touched from more than one OS thread: a callback invoked from
/// `deliver()` can reentrantly call back into `submit()`/`flush()`/
/// `process_events()` on this same Context (nothing stops user code from
/// doing that), and PyO3 enforces Rust's exclusive-borrow rule on
/// `&mut self` pyclass methods at runtime - a reentrant call while an
/// outer `&mut self` call was still "on the stack" would hit a spurious
/// "already borrowed" error. Locking a plain `Mutex` internally (and
/// releasing it *before* invoking any callback - see `deliver`'s own doc
/// comment) sidesteps that entirely, matching `thread_aio`/`linux_aio`'s
/// own bridges.
#[gen_stub_pyclass]
#[pyclass(name = "Context", module = "linux_uring", weakref)]
pub struct AIOContext {
    #[pyo3(get)]
    max_requests: u32,
    engine: Mutex<Engine<UringDriver>>,
    registry: Mutex<HashMap<RequestId, Py<AIOOperation>>>,
    /// Real completions already drained from the ring (via `engine.poll()`,
    /// which also filters out `ASYNC_CANCEL` sentinel CQEs - see
    /// `caio-backend-uring::driver::poll`) but not yet delivered to Python,
    /// because a `max_requests`-bounded `process_events()` call already
    /// claimed its share this round. `flush()`/the next `process_events()`
    /// call drains this *before* anything newly polled, oldest first -
    /// nothing is ever silently dropped, only deferred.
    pending: Mutex<VecDeque<(RequestId, CompletionResult)>>,
}

impl AIOContext {
    /// Combines whatever's already sitting in `pending` (oldest first)
    /// with a fresh, non-blocking `engine.poll()`, delivers at most
    /// `max` of the result (all of it if `None`), and leaves anything
    /// beyond that still in `pending` for a later call. This is the one
    /// place completions actually reach Python, so both `flush()` and
    /// `process_events()` go through it - `max_requests` genuinely bounds
    /// how many callbacks run per call, not just the returned count.
    fn drain_bounded(&self, py: Python<'_>, max: Option<usize>) -> usize {
        let mut pending = self.pending.lock().unwrap();
        pending.extend(self.engine.lock().unwrap().poll());

        let take = max.unwrap_or(pending.len()).min(pending.len());
        let ready: Vec<_> = pending.drain(..take).collect();
        drop(pending);

        deliver(py, &self.registry, ready);
        take
    }
}

#[gen_stub_pymethods]
#[pymethods]
impl AIOContext {
    #[new]
    #[pyo3(signature = (max_requests=0, sqpoll=false))]
    fn new(max_requests: u32, sqpoll: bool) -> PyResult<Self> {
        let max_requests = if max_requests == 0 { CTX_MAX_REQUESTS_DEFAULT } else { max_requests };
        let driver =
            UringDriver::new(max_requests, sqpoll).map_err(|e| PySystemError::new_err(e.to_string()))?;
        let engine = Engine::new(next_context_id(), max_requests as usize, driver);
        Ok(AIOContext {
            max_requests,
            engine: Mutex::new(engine),
            registry: Mutex::new(HashMap::new()),
            pending: Mutex::new(VecDeque::new()),
        })
    }

    fn __repr__(&self) -> String {
        format!("<Context: max_requests={}>", self.max_requests)
    }

    #[getter]
    fn fileno(&self) -> i32 {
        self.engine.lock().unwrap().driver_mut().eventfd()
    }

    // `sqe.fd` is the caller's raw fd, written into the SQE as-is -
    // deliberately not `dup()`-ed (see `AbstractOperation` in
    // caio/abstract.py for the documented fd lifetime contract).
    #[pyo3(signature = (*ops))]
    fn submit(
        &self,
        #[gen_stub(override_type(type_repr = "Operation"))] ops: &Bound<'_, PyTuple>,
    ) -> PyResult<u32> {
        if ops.is_empty() {
            return Ok(0);
        }
        let py = ops.py();

        // `already_submitted()` alone only catches an Operation reused
        // *across* separate submit() calls - `mark_submitted()` doesn't run
        // until after this whole loop, so two references to the very same
        // object *within this one call* (`ctx.submit(op, op)`) would both
        // still read NEW here and both get accepted with two different
        // RequestIds. `seen` tracks Python object identity within this
        // call only, closing that gap.
        let mut specs = Vec::with_capacity(ops.len());
        let mut op_pys = Vec::with_capacity(ops.len());
        let mut seen = HashSet::with_capacity(ops.len());
        for item in ops.iter() {
            let op_bound = item.cast::<AIOOperation>().map_err(|_| PyTypeError::new_err("argument is not an Operation"))?;
            if op_bound.borrow().already_submitted() {
                continue;
            }
            if !seen.insert(op_bound.as_ptr() as usize) {
                continue;
            }
            let spec = op_bound.borrow().build_spec(py)?;
            specs.push(spec);
            op_pys.push(op_bound.clone().unbind());
        }

        if specs.is_empty() {
            return Ok(0);
        }

        let report = self.engine.lock().unwrap().submit_many(specs);

        {
            let mut registry = self.registry.lock().unwrap();
            for (op_py, handle) in op_pys.iter().zip(report.accepted.iter()) {
                let op_bound = op_py.bind(py);
                op_bound.borrow().mark_submitted(*handle);
                registry.insert(handle.request_id(), op_py.clone_ref(py));
            }
        }

        let submitted = report.accepted.len() as u32;

        // A prepare-time failure (the SQE's own read/write buffer
        // allocation - see `UringSubmission::from_spec` - failing, e.g.
        // under a tight RLIMIT_AS) is a real, actionable error distinct
        // from ordinary ring-full backpressure, and must surface as one
        // rather than being silently absorbed into a lower accepted count.
        if let Some(detail) = report.rejected.iter().find_map(|r| match &r.reason {
            RejectReason::PrepareFailed(detail) => Some(detail.clone()),
            _ => None,
        }) {
            return Err(pyo3::exceptions::PyMemoryError::new_err(detail));
        }

        let overflow = report
            .rejected
            .iter()
            .any(|r| matches!(r.reason, RejectReason::CapacityExceeded | RejectReason::NotAttempted));
        if overflow {
            return Err(PyOverflowError::new_err("io_uring SQ ring full"));
        }

        Ok(submitted)
    }

    /// Submits all pending SQEs to the kernel in one `io_uring_enter` call
    /// (or wakes the SQPOLL thread), then drains whatever completed inline
    /// - including anything left over from a previous `max_requests`-
    /// bounded `process_events()` call (see `drain_bounded`'s own doc
    /// comment) - unconditionally, this method has no `max_requests` of
    /// its own to respect.
    fn flush(&self, py: Python<'_>) -> PyResult<usize> {
        self.engine.lock().unwrap().driver_mut().flush().map_err(|e| PySystemError::new_err(e.to_string()))?;
        Ok(self.drain_bounded(py, None))
    }

    #[pyo3(signature = (operation))]
    fn cancel(&self, operation: &Bound<'_, AIOOperation>) -> PyResult<i64> {
        // Best-effort, fire-and-forget: the target operation's own
        // completion (cancelled or not) always shows up through the
        // normal poll() path, so nothing further is delivered
        // synchronously here.
        if let Some(id) = operation.borrow().request_id() {
            let _ = self.engine.lock().unwrap().cancel(id);
        }
        Ok(0)
    }

    #[pyo3(signature = (max_requests=0, min_requests=0, timeout=0))]
    fn process_events(&self, py: Python<'_>, max_requests: u32, min_requests: u32, timeout: i32) -> PyResult<usize> {
        let max_requests = if max_requests == 0 { EV_MAX_REQUESTS_DEFAULT } else { max_requests };

        if min_requests > max_requests {
            return Err(PyValueError::new_err(format!(
                "min_requests ({}) must be <= max_requests ({})",
                min_requests, max_requests,
            )));
        }

        if min_requests > 0 {
            // Plain io_uring_enter(GETEVENTS) has no timeout parameter of
            // its own - a GIL-released sleep-poll loop against a
            // wall-clock deadline instead (the driver crate has no notion
            // of the GIL to release itself, so the bridge owns this).
            // Waits for *real* completions accumulated into `pending`, not
            // raw CQE ring occupancy (`cq_available()`): a stray
            // `ASYNC_CANCEL` sentinel CQE satisfies the ring's own notion
            // of "something completed" without being a real completion
            // `poll()` would ever hand back, which would otherwise let
            // this return early having delivered fewer real completions
            // than `min_requests` asked for.
            let wait_secs = if timeout > 0 { timeout as u64 } else { 0 };
            let deadline = Instant::now() + Duration::from_secs(wait_secs);
            py.detach(|| -> PyResult<()> {
                loop {
                    let ready = {
                        let mut pending = self.pending.lock().unwrap();
                        pending.extend(self.engine.lock().unwrap().poll());
                        pending.len()
                    };
                    if ready >= min_requests as usize || Instant::now() >= deadline {
                        return Ok(());
                    }
                    self.engine
                        .lock()
                        .unwrap()
                        .driver_mut()
                        .pump_getevents()
                        .map_err(|e| PySystemError::new_err(e.to_string()))?;
                    std::thread::sleep(Duration::from_millis(1));
                }
            })?;
        }

        // max_requests genuinely bounds how many callbacks run in this one
        // call, not just the returned count (see `drain_bounded`'s own doc
        // comment) - anything beyond it stays in `pending` for a later
        // `flush()`/`process_events()` call instead of running now.
        Ok(self.drain_bounded(py, Some(max_requests as usize)))
    }

    fn poll(&self) -> PyResult<u64> {
        self.engine
            .lock()
            .unwrap()
            .driver_mut()
            .read_eventfd()
            .map_err(|e| PySystemError::new_err(e.to_string()))?
            .ok_or_else(|| pyo3::exceptions::PyBlockingIOError::new_err(()))
    }
}

impl Drop for AIOContext {
    fn drop(&mut self) {
        let outstanding = self.engine.lock().unwrap().outstanding_count();
        if outstanding == 0 {
            self.engine.lock().unwrap().shutdown();
            return;
        }

        // Reap every outstanding operation before tearing anything down -
        // unmapping the CQ ring while the kernel might still write a
        // completion into it would be a use-after-free at the kernel
        // level. Bounded by a wall-clock deadline (not just by
        // `outstanding_count()`), and looping on that count itself rather
        // than a single `poll()` round's return value - see
        // `DROP_REAP_TIMEOUT_SECS`'s own doc comment in
        // caio-backend-uring for why both matter (no timeout on a raw
        // io_uring_enter(GETEVENTS) call, and a stray ASYNC_CANCEL
        // sentinel CQE satisfying io_uring's min_complete without being a
        // real completion).
        Python::attach(|py| {
            let _ = self.engine.lock().unwrap().driver_mut().flush();

            let deadline = Instant::now() + Duration::from_secs(DROP_REAP_TIMEOUT_SECS);
            let mut timed_out = false;
            while self.engine.lock().unwrap().outstanding_count() > 0 && !timed_out {
                let got_something = py.detach(|| loop {
                    if self.engine.lock().unwrap().driver_mut().cq_available() > 0 {
                        return true;
                    }
                    if Instant::now() >= deadline {
                        return false;
                    }
                    let _ = self.engine.lock().unwrap().driver_mut().pump_getevents();
                    std::thread::sleep(Duration::from_millis(1));
                });

                if got_something {
                    // Reaped, but never delivered to Python: a destructor
                    // is not a safe place to run arbitrary callback code -
                    // just drop the completions, do not call `deliver()`.
                    let _ = self.engine.lock().unwrap().poll();
                } else {
                    timed_out = true;
                }
            }

            if timed_out {
                eprintln!(
                    "caio.linux_uring: Context dropped with {} operation(s) still in flight after {}s; \
                     giving up and closing the io_uring fd anyway",
                    self.engine.lock().unwrap().outstanding_count(),
                    DROP_REAP_TIMEOUT_SECS,
                );
            }
        });

        self.engine.lock().unwrap().shutdown();
    }
}
