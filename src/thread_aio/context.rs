use std::collections::{HashMap, HashSet};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, Weak};

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyTuple;
use pyo3_stub_gen::derive::{gen_stub_pyclass, gen_stub_pymethods};

use caio_core::{CancelStatus, ContextId, Engine, RejectReason, RequestId};
use caio_backend_thread::ThreadDriver;

use crate::operation::AIOOperation;

const CTX_POOL_SIZE_DEFAULT: u16 = 8;
const CTX_MAX_REQUESTS_DEFAULT: u16 = 512;
const MAX_THREADS: u16 = 128;
const MAX_QUEUE: u32 = 65536;

fn next_context_id() -> ContextId {
    static NEXT: AtomicU64 = AtomicU64::new(1);
    ContextId::new(NEXT.fetch_add(1, Ordering::Relaxed))
}

/// Owns the engine and the `RequestId -> Operation` registry the worker
/// threads' wake callback needs (see `wake()` below). Held behind `Arc` so
/// that callback - running on a worker thread, not the one that called
/// `submit()` - can reach the same engine/registry the `Context` uses.
struct Shared {
    engine: Mutex<Engine<ThreadDriver>>,
    registry: Mutex<HashMap<RequestId, Py<AIOOperation>>>,
}

/// Reacquires the GIL and delivers whatever completions are currently
/// available, on whatever thread calls this - a worker thread immediately
/// after finishing a job (via `ThreadDriver`'s `on_event`), preserving this
/// backend's existing zero-added-latency callback behavior instead of
/// requiring a separate poll step. Locks `engine` before `registry`,
/// consistently with every other path that touches both, to avoid a
/// lock-ordering deadlock.
fn wake(shared: &Shared) {
    Python::attach(|py| {
        let completions = shared.engine.lock().unwrap().poll();
        if completions.is_empty() {
            return;
        }
        let mut registry = shared.registry.lock().unwrap();
        let ops: Vec<_> = completions
            .into_iter()
            .filter_map(|(id, result)| registry.remove(&id).map(|op| (op, result)))
            .collect();
        drop(registry);

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
    });
}

/// thread aio context
#[gen_stub_pyclass]
#[pyclass(name = "Context", module = "thread_aio", weakref)]
pub struct AIOContext {
    #[pyo3(get)]
    max_requests: u16,
    #[pyo3(get)]
    pool_size: u16,
    shared: Arc<Shared>,
}

#[gen_stub_pymethods]
#[pymethods]
impl AIOContext {
    #[new]
    #[pyo3(signature = (max_requests=0, pool_size=0))]
    fn new(max_requests: u16, pool_size: u16) -> PyResult<Self> {
        let max_requests = if max_requests == 0 { CTX_MAX_REQUESTS_DEFAULT } else { max_requests };
        let pool_size = if pool_size == 0 { CTX_POOL_SIZE_DEFAULT } else { pool_size };

        if pool_size > MAX_THREADS {
            return Err(PyValueError::new_err(format!(
                "pool_size too large. Allowed lower then {}",
                MAX_THREADS,
            )));
        }

        if max_requests as u32 >= MAX_QUEUE - 1 {
            return Err(PyValueError::new_err(format!(
                "max_requests too large. Allowed lower then {}",
                MAX_QUEUE - 1,
            )));
        }

        // `Arc::new_cyclic` lets the wake closure capture a `Weak<Shared>`
        // pointing at a `Shared` that doesn't exist yet: `ThreadDriver`
        // needs the closure to construct `Engine`, but `Shared` needs the
        // finished `Engine` to construct itself - `Weak` breaks that
        // chicken-and-egg cycle. `upgrade()` returning `None` (already
        // fully dropped) is intentionally a silent no-op, not reachable in
        // correct use: `Drop` below joins every worker thread (via
        // `shutdown()`) before the last strong `Arc<Shared>` reference can
        // go away, so no worker can still be calling this after that point.
        let shared = Arc::new_cyclic(|weak: &Weak<Shared>| {
            let weak_for_wake = weak.clone();
            let on_event: Arc<dyn Fn() + Send + Sync> = Arc::new(move || {
                if let Some(shared) = weak_for_wake.upgrade() {
                    wake(&shared);
                }
            });
            let driver = ThreadDriver::new(pool_size as usize, max_requests as usize, on_event);
            let engine = Engine::new(next_context_id(), max_requests as usize, driver);
            Shared { engine: Mutex::new(engine), registry: Mutex::new(HashMap::new()) }
        });

        Ok(AIOContext { max_requests, pool_size, shared })
    }

    fn __repr__(&self) -> String {
        format!(
            "<Context: max_requests={}, pool_size={}>",
            self.max_requests, self.pool_size,
        )
    }

    #[pyo3(signature = (*ops))]
    fn submit(
        &self,
        #[gen_stub(override_type(type_repr = "Operation"))] ops: &Bound<'_, PyTuple>,
    ) -> PyResult<usize> {
        let py = ops.py();

        // Two passes: validate/build everything fallible first (type
        // checks, spec construction) without touching the engine or any
        // Operation's own state, so a mid-batch failure here can't leave
        // an earlier item half-accepted. An already-submitted Operation is
        // silently skipped rather than erroring.
        //
        // `already_submitted()` alone only catches an Operation reused
        // *across* separate submit() calls - `mark_submitted()` doesn't run
        // until after this whole loop, so two references to the very same
        // object *within this one call* (`ctx.submit(op, op)`) would both
        // still read NEW here and both get accepted with two different
        // RequestIds, corrupting the object's single `request_id` field and
        // running its I/O twice. `seen` tracks Python object identity
        // within this call only, closing that gap.
        let mut specs = Vec::with_capacity(ops.len());
        let mut op_pys = Vec::with_capacity(ops.len());
        let mut seen = HashSet::with_capacity(ops.len());
        for (index, item) in ops.iter().enumerate() {
            let op_bound = item
                .cast::<AIOOperation>()
                .map_err(|_| PyTypeError::new_err(format!("Wrong type for argument {}", index)))?;
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

        let report = self.shared.engine.lock().unwrap().submit_many(specs);

        {
            let mut registry = self.shared.registry.lock().unwrap();
            for (op_py, handle) in op_pys.iter().zip(report.accepted.iter()) {
                op_py.bind(py).borrow().mark_submitted(*handle);
                registry.insert(handle.request_id(), op_py.clone_ref(py));
            }
        }

        let submitted = report.accepted.len();

        // A prepare-time failure (the job's own read/write buffer
        // allocation - see `ThreadDriver::Job::from_spec` - failing, e.g.
        // under a tight RLIMIT_AS) is a real, actionable error distinct
        // from ordinary queue-full backpressure, and must surface as one
        // rather than being silently absorbed into a lower accepted count.
        if let Some(detail) = report.rejected.iter().find_map(|r| match &r.reason {
            RejectReason::PrepareFailed(detail) => Some(detail.clone()),
            _ => None,
        }) {
            return Err(pyo3::exceptions::PyMemoryError::new_err(detail));
        }

        let capacity_exceeded = report
            .rejected
            .iter()
            .any(|r| matches!(r.reason, RejectReason::CapacityExceeded | RejectReason::NotAttempted));
        if capacity_exceeded {
            return Err(pyo3::exceptions::PyRuntimeError::new_err("Thread pool queue full"));
        }

        Ok(submitted)
    }

    /// Always ends up reporting 0 cancelled: `ThreadDriver` genuinely can't
    /// interrupt a dispatched request (`supports_inflight_cancel()` is
    /// `false`, so `Engine::cancel()` always answers `Unsupported`).
    #[pyo3(signature = (*ops))]
    fn cancel(&self, #[gen_stub(override_type(type_repr = "Operation"))] ops: &Bound<'_, PyTuple>) -> usize {
        let mut cancelled = 0usize;
        for item in ops.iter() {
            let Ok(op_bound) = item.cast::<AIOOperation>() else { continue };
            let Some(id) = op_bound.borrow().request_id() else { continue };
            let Ok(status) = self.shared.engine.lock().unwrap().cancel(id) else { continue };
            if matches!(status, CancelStatus::Requested | CancelStatus::CancelledBeforeStart) {
                cancelled += 1;
            }
        }
        cancelled
    }
}

impl Drop for AIOContext {
    fn drop(&mut self) {
        // Worker threads may need the GIL to run this Context's own wake()
        // callback while we're joining them, so release it for the
        // duration - otherwise a Context dropped from a GIL-holding thread
        // while operations are still in flight would deadlock.
        Python::attach(|py| {
            py.detach(|| {
                // Deliberately NOT `self.shared.engine.lock().unwrap().shutdown()`:
                // that would hold `engine`'s lock for the entire blocking
                // join below, and a worker thread's own `wake()` (called
                // right before it can exit) needs that same lock to
                // deliver its last completion - holding it here would
                // deadlock against exactly that worker. Signal shutdown
                // and take the join handles under a brief lock, then join
                // them with no lock held at all - see
                // `ThreadDriver::signal_shutdown`/`take_workers`'s own doc
                // comments.
                let workers = {
                    let mut engine = self.shared.engine.lock().unwrap();
                    let driver = engine.driver_mut();
                    driver.signal_shutdown();
                    driver.take_workers()
                };
                // Bounded: a worker stuck in a blocking pread/pwrite/fsync
                // against a hung device or network filesystem must not
                // hang this destructor forever (`Driver::shutdown()`'s own
                // documented contract - see caio-core::Driver) - past
                // `DROP_REAP_TIMEOUT_SECS`, give up waiting and let the
                // process/interpreter move on; the worker (and whatever it
                // was doing) finishes on its own later, it just isn't
                // waited for here anymore.
                let still_running = caio_backend_thread::join_workers_with_deadline(
                    workers,
                    std::time::Duration::from_secs(caio_backend_thread::DROP_REAP_TIMEOUT_SECS),
                );
                if still_running > 0 {
                    eprintln!(
                        "caio.thread_aio: Context dropped with {still_running} worker(s) still \
                         running after {}s; giving up and returning anyway",
                        caio_backend_thread::DROP_REAP_TIMEOUT_SECS,
                    );
                }
            });
        });
    }
}
