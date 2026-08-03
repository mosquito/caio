use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;

use pyo3::exceptions::{PyBlockingIOError, PyOverflowError, PySystemError, PyTypeError, PyValueError};
use pyo3::gc::PyVisit;
use pyo3::prelude::*;
use pyo3::types::PyTuple;
use pyo3::PyTraverseError;
use pyo3_stub_gen::derive::{gen_stub_pyclass, gen_stub_pymethods};

use caio_core::{CompletionResult, ContextId, Engine, RejectReason, RequestId};
use caio_backend_linux_aio::LinuxAioDriver;

use crate::operation::{io_submit_error, AIOOperation};

const CTX_MAX_REQUESTS_DEFAULT: u32 = 32;
const EV_MAX_REQUESTS_DEFAULT: u32 = 512;
/// Hard upper bound on `process_events(max_requests=...)`: this argument
/// directly sizes an events buffer `io_getevents()` allocates up front, so
/// an unbounded caller-controlled value could request an enormous
/// allocation well before the kernel's own `io_context` depth would ever
/// actually produce that many completions at once.
const MAX_EVENTS_REQUEST: u32 = 1 << 20;

fn next_context_id() -> ContextId {
    static NEXT: AtomicU64 = AtomicU64::new(1);
    ContextId::new(NEXT.fetch_add(1, Ordering::Relaxed))
}

/// linux aio context representation
#[gen_stub_pyclass]
#[pyclass(name = "Context", module = "linux_aio", weakref)]
pub struct AIOContext {
    #[pyo3(get)]
    max_requests: u32,
    engine: Mutex<Engine<LinuxAioDriver>>,
    registry: Mutex<HashMap<RequestId, Py<AIOOperation>>>,
}

/// Applies whatever completions are currently available and invokes their
/// callbacks - shared by `cancel()` (a cancelled request's outcome is
/// delivered synchronously by `io_cancel()` itself, not through a later
/// `io_getevents()` call) and `process_events()`.
///
/// Commits state (removing each completed request from the registry)
/// *before* invoking any callback - a callback that reentrantly calls back
/// into `submit()`/`cancel()`/`process_events()` on this same Context must
/// see already-committed state, not reacquire a lock this same call still
/// holds. Concretely: the engine lock is taken, `poll()`'d, and released
/// *before* this function is ever called with the resulting completions -
/// never held across a callback invocation.
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

#[gen_stub_pymethods]
#[pymethods]
impl AIOContext {
    #[new]
    #[pyo3(signature = (max_requests=0))]
    fn new(max_requests: u32) -> PyResult<Self> {
        let max_requests = if max_requests == 0 { CTX_MAX_REQUESTS_DEFAULT } else { max_requests };

        let driver = LinuxAioDriver::new(max_requests).map_err(|e| PySystemError::new_err(e.to_string()))?;
        let engine = Engine::new(next_context_id(), max_requests as usize, driver);

        Ok(AIOContext { max_requests, engine: Mutex::new(engine), registry: Mutex::new(HashMap::new()) })
    }

    #[getter]
    fn fileno(&self) -> i32 {
        self.engine.lock().unwrap().driver_mut().eventfd()
    }

    fn __repr__(&self) -> String {
        format!("<Context: max_requests={}>", self.max_requests)
    }

    #[pyo3(signature = (*ops))]
    fn submit(
        slf: &Bound<'_, Self>,
        #[gen_stub(override_type(type_repr = "Operation"))] ops: &Bound<'_, PyTuple>,
    ) -> PyResult<usize> {
        let py = ops.py();
        let ctx_ref = slf.borrow();

        // Validate every argument's type up front, before touching any
        // operation's state - a later-in-the-batch type error must not
        // leave earlier, already-claimed operations permanently stuck.
        //
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
            let op_bound = item.cast::<AIOOperation>().map_err(|_| PyTypeError::new_err("Wrong type for argument"))?;
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

        let report = ctx_ref.engine.lock().unwrap().submit_many(specs);

        {
            let mut registry = ctx_ref.registry.lock().unwrap();
            for (op_py, handle) in op_pys.iter().zip(report.accepted.iter()) {
                let op_bound = op_py.bind(py);
                op_bound.borrow().mark_submitted(*handle);
                op_bound.borrow().set_context(slf.as_any());
                registry.insert(handle.request_id(), op_py.clone_ref(py));
            }
        }

        // A prepare-time failure (the request's own read/write buffer
        // allocation - see `InflightRequest::from_spec` - failing, e.g.
        // under a tight RLIMIT_AS) is a real, actionable error distinct
        // from ordinary queue-full backpressure, and must surface as one
        // rather than being silently absorbed into a lower accepted count.
        if let Some(detail) = report.rejected.iter().find_map(|r| match &r.reason {
            RejectReason::PrepareFailed(detail) => Some(detail.clone()),
            _ => None,
        }) {
            return Err(pyo3::exceptions::PyMemoryError::new_err(detail));
        }

        // A dispatch-time failure (io_submit() rejecting a specific
        // request - bad fd, invalid params) is the one rejection reason
        // that must still surface as a raised exception. Capacity
        // exhaustion must not raise - it's reported through the returned
        // count instead (accepted fewer than requested).
        if let Some(dispatch_error) =
            report.rejected.iter().find_map(|r| match &r.reason {
                RejectReason::DispatchFailed(os_error) => Some(os_error.errno),
                _ => None,
            })
        {
            return Err(io_submit_error(dispatch_error));
        }

        Ok(report.accepted.len())
    }

    #[pyo3(signature = (operation))]
    fn cancel(&self, py: Python<'_>, operation: &Bound<'_, AIOOperation>) -> PyResult<i64> {
        let Some(handle) = operation.borrow().request_id() else {
            return Err(PyValueError::new_err("operation was never submitted"));
        };

        // `_status` itself isn't surfaced separately: the return value is
        // whatever result the cancel attempt produced (0 if none). A
        // `WrongContext` error (the operation belongs to some other
        // Context) surfaces through this same message: from the caller's
        // point of view it's exactly as untracked as an unknown request.
        let _status = self
            .engine
            .lock()
            .unwrap()
            .cancel(handle)
            .map_err(|_| PyValueError::new_err("operation is not tracked by this Context"))?;

        // A successful cancel delivers its outcome synchronously (see
        // `LinuxAioDriver::cancel_inflight`'s own doc comment) - poll()
        // right away so it's available to hand back below, instead of
        // waiting for some later process_events() call.
        let id = handle.request_id();
        let completions = self.engine.lock().unwrap().poll();
        let result = completions.iter().find(|(cid, _)| *cid == id).map(|(_, result)| match result {
            CompletionResult::Write { transferred } | CompletionResult::Read { transferred, .. } => {
                *transferred as i64
            }
            CompletionResult::Sync | CompletionResult::Cancelled => 0,
            CompletionResult::Error(e) => -(e.errno as i64),
        });
        deliver(py, &self.registry, completions);

        Ok(result.unwrap_or(0))
    }

    #[pyo3(signature = (max_requests=0, min_requests=0, timeout=0))]
    fn process_events(
        &self, py: Python<'_>, max_requests: u32, min_requests: u32, timeout: i32,
    ) -> PyResult<usize> {
        let max_requests = if max_requests == 0 { EV_MAX_REQUESTS_DEFAULT } else { max_requests };

        if max_requests > MAX_EVENTS_REQUEST {
            return Err(PyOverflowError::new_err(format!(
                "max_requests ({max_requests}) exceeds the maximum allowed ({MAX_EVENTS_REQUEST})",
            )));
        }
        if min_requests > max_requests {
            return Err(PyValueError::new_err(format!(
                "min_requests \"{}\" must be lower then max_requests \"{}\"",
                min_requests, max_requests,
            )));
        }
        // A negative timeout, cast straight to `libc::time_t` as `tv_sec`,
        // would wrap around to a huge unsigned duration once
        // `caio-backend-linux-aio::abi::io_getevents` computes its
        // deadline (`tv_sec as u64`) - `Instant::now() + that` can panic
        // outright (checked_add there is defense in depth, not a
        // substitute for rejecting the bad input here at the boundary).
        if timeout < 0 {
            return Err(PyValueError::new_err(format!("timeout ({timeout}) must not be negative")));
        }

        let ts = libc::timespec { tv_sec: timeout as libc::time_t, tv_nsec: 0 };

        // io_getevents' timeout is honored natively by the kernel - the
        // only bug this guards against is holding the GIL for this call's
        // whole (potentially `timeout`-seconds-long) duration, freezing
        // the entire interpreter rather than just this thread.
        let count = py
            .detach(|| {
                self.engine.lock().unwrap().driver_mut().wait_for_events(
                    min_requests as usize,
                    max_requests as usize,
                    Some(ts),
                )
            })
            .map_err(|e| {
                if e.kind() == std::io::ErrorKind::OutOfMemory {
                    pyo3::exceptions::PyMemoryError::new_err(e.to_string())
                } else {
                    PySystemError::new_err(e.to_string())
                }
            })?;

        let completions = self.engine.lock().unwrap().poll();
        deliver(py, &self.registry, completions);

        Ok(count)
    }

    fn poll(&self) -> PyResult<u64> {
        self.engine
            .lock()
            .unwrap()
            .driver_mut()
            .read_eventfd()
            .map_err(|e| PySystemError::new_err(e.to_string()))?
            .ok_or_else(|| PyBlockingIOError::new_err(()))
    }

    // CPython GC slot methods - see `AIOOperation`'s matching impl in
    // operation.rs for the reference cycle this closes (`registry` here
    // is the other half: Context -> registry -> Operation -> context ->
    // Context).
    #[gen_stub(skip)]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Ok(registry) = self.registry.lock() {
            for op in registry.values() {
                visit.call(op)?;
            }
        }
        Ok(())
    }

    #[gen_stub(skip)]
    fn __clear__(&mut self) {
        self.registry.get_mut().unwrap().clear();
    }
}

impl Drop for AIOContext {
    fn drop(&mut self) {
        // Outstanding requests are not reaped here. Ordinarily this can't
        // fire with anything left outstanding - Operation's own `context`
        // field keeps this Context alive for as long as any Operation
        // submitted through it exists - but `__traverse__`/`__clear__` let
        // Python's cyclic GC break that cycle when both sides are
        // otherwise unreachable, which can run this Drop with a request
        // still genuinely outstanding at the kernel level. Still safe:
        // `io_destroy()` blocks until any outstanding iocbs actually
        // finish before tearing down the AIO context.
        self.engine.lock().unwrap().shutdown();
    }
}
