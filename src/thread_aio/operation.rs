use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU64, Ordering};
use std::sync::Mutex;

use pyo3::exceptions::{PyMemoryError, PyOverflowError, PyRuntimeError, PySystemError, PyValueError};
use pyo3::types::{PyBytes, PyMemoryView};
use pyo3::prelude::*;
use pyo3_stub_gen::derive::{gen_stub_pyclass, gen_stub_pymethods};

use caio_core::{CompletionResult, OpCode, RequestHandle, RequestSpec};

fn check_transfer_size(nbytes: u64) -> PyResult<()> {
    if nbytes > caio_core::MAX_TRANSFER_SIZE {
        return Err(PyOverflowError::new_err(format!(
            "nbytes ({nbytes}) exceeds the maximum single-operation transfer size ({})",
            caio_core::MAX_TRANSFER_SIZE,
        )));
    }
    Ok(())
}

/// thread aio operation representation.
///
/// One-shot: `request_id` starts `None` and is set exactly once, the
/// moment `Context::submit()` gets this operation accepted by the engine.
/// `submit()` checks this before even building a `RequestSpec` and rejects
/// anything that already has one - there is no separate "already used"
/// flag because `request_id.is_some()` already is that flag. An operation
/// that was attempted but rejected (e.g. the pool's queue was full) never
/// gets a `request_id`, so it stays fully retryable.
#[gen_stub_pyclass]
#[pyclass(name = "Operation", module = "thread_aio", weakref)]
pub struct AIOOperation {
    opcode: OpCode,
    #[pyo3(get)]
    fileno: u32,
    #[pyo3(get)]
    offset: u64,
    requested_nbytes: u64,
    /// The original `bytes` object passed to `write()`, kept alive so the
    /// `.payload` property can return the same object identity and so a
    /// later `build_spec()` call can re-copy it - `Operation` itself is
    /// immutable and reusable as a template; only the resulting request is
    /// one-shot.
    write_payload: Option<Py<PyBytes>>,
    priority: u16,

    /// Empty until the real completion arrives (see `apply_result`) - not
    /// eagerly allocated in `new_base` (a caller-controlled `nbytes` up to
    /// `MAX_TRANSFER_SIZE`, just under 2 GiB, would otherwise be allocated
    /// twice: once here as a placeholder, once more by the driver's own
    /// job buffer that the worker thread actually reads into).
    result_buf: Mutex<Vec<u8>>,
    /// Read: `requested_nbytes` until completion, then the actual transfer
    /// count (may be less on a short read) - reported by the `nbytes`
    /// getter without needing to lock `result_buf` (which, pre-completion,
    /// is empty rather than placeholder-sized). Write/Fsync/Fdsync:
    /// unused, `nbytes` reports `requested_nbytes` directly.
    transferred_nbytes: AtomicU64,
    result: AtomicI32,
    error: AtomicI32,
    callback: Mutex<Option<Py<PyAny>>>,
    request_id: Mutex<Option<RequestHandle>>,
    /// True from acceptance until `apply_result` runs - gates
    /// `payload`/`get_value()` so they can't observe a buffer the worker
    /// thread may still be writing into.
    in_flight: AtomicBool,
}

impl AIOOperation {
    fn new_base(
        opcode: OpCode, fileno: u32, offset: u64, requested_nbytes: u64, write_payload: Option<Py<PyBytes>>,
        priority: u16,
    ) -> Self {
        AIOOperation {
            opcode,
            fileno,
            offset,
            requested_nbytes,
            write_payload,
            priority,
            result_buf: Mutex::new(Vec::new()),
            transferred_nbytes: AtomicU64::new(requested_nbytes),
            result: AtomicI32::new(0),
            error: AtomicI32::new(0),
            callback: Mutex::new(None),
            request_id: Mutex::new(None),
            in_flight: AtomicBool::new(false),
        }
    }

    fn check_not_in_flight(&self) -> PyResult<()> {
        if self.in_flight.load(Ordering::SeqCst) {
            return Err(PyRuntimeError::new_err(
                "operation is still in flight; wait for completion before accessing payload/get_value",
            ));
        }
        Ok(())
    }

    pub(crate) fn already_submitted(&self) -> bool {
        self.request_id.lock().unwrap().is_some()
    }

    pub(crate) fn request_id(&self) -> Option<RequestHandle> {
        *self.request_id.lock().unwrap()
    }

    /// Builds a fresh `RequestSpec` from this operation's own stored
    /// fields - never consumes anything: a rejected submission attempt
    /// (this spec never actually reaching the engine's registry) must
    /// leave this operation fully retryable, and a rebuilt spec from the
    /// same source fields is how that "retry with the original data"
    /// works.
    pub(crate) fn build_spec(&self, py: Python<'_>) -> PyResult<RequestSpec> {
        let spec = match self.opcode {
            OpCode::Read => RequestSpec::read(self.fileno as i32, self.offset, self.requested_nbytes, self.priority),
            OpCode::Write => {
                // Fallible copy: crossing from Python-GC'd memory into a
                // Rust-owned allocation is unavoidable here, but the copy's
                // size is caller-controlled - an infallible `.to_vec()`
                // would abort the whole process on OOM instead of
                // surfacing a catchable error.
                let bytes = self
                    .write_payload
                    .as_ref()
                    .expect("Write operations always carry a payload")
                    .bind(py)
                    .as_bytes();
                let mut payload = Vec::new();
                payload.try_reserve_exact(bytes.len()).map_err(|e| {
                    PyMemoryError::new_err(format!("allocating {}-byte write payload: {e}", bytes.len()))
                })?;
                payload.extend_from_slice(bytes);
                RequestSpec::write(self.fileno as i32, self.offset, payload.into_boxed_slice(), self.priority)
            }
            OpCode::Fsync => Ok(RequestSpec::fsync(self.fileno as i32, self.priority)),
            OpCode::Fdsync => Ok(RequestSpec::fdsync(self.fileno as i32, self.priority)),
        };
        spec.map_err(|e| PyOverflowError::new_err(e.to_string()))
    }

    /// Marks this operation as accepted by the engine - called only for
    /// requests that actually made it into `SubmitReport::accepted`, never
    /// for rejected ones (see the struct's own doc comment).
    pub(crate) fn mark_submitted(&self, handle: RequestHandle) {
        *self.request_id.lock().unwrap() = Some(handle);
        self.in_flight.store(true, Ordering::SeqCst);
    }

    pub(crate) fn take_callback(&self) -> Option<Py<PyAny>> {
        self.callback.lock().unwrap().take()
    }

    pub(crate) fn result_value(&self) -> i32 {
        self.result.load(Ordering::SeqCst)
    }

    /// Applies a terminal `CompletionResult` from the engine. `Cancelled`
    /// can't actually happen for this backend (`supports_inflight_cancel()`
    /// is `false`), but is handled like any other failure rather than left
    /// as `unreachable!()` - a future capability change shouldn't turn
    /// into a panic here.
    pub(crate) fn apply_result(&self, result: CompletionResult) {
        self.in_flight.store(false, Ordering::SeqCst);
        match result {
            CompletionResult::Read { buffer, transferred } => {
                *self.result_buf.lock().unwrap() = buffer.into_vec();
                self.transferred_nbytes.store(transferred as u64, Ordering::SeqCst);
                self.result.store(transferred as i32, Ordering::SeqCst);
                self.error.store(0, Ordering::SeqCst);
            }
            CompletionResult::Write { transferred } => {
                self.result.store(transferred as i32, Ordering::SeqCst);
                self.error.store(0, Ordering::SeqCst);
            }
            CompletionResult::Sync => {
                self.result.store(0, Ordering::SeqCst);
                self.error.store(0, Ordering::SeqCst);
            }
            CompletionResult::Cancelled => {
                self.result.store(-1, Ordering::SeqCst);
                self.error.store(-1, Ordering::SeqCst);
            }
            CompletionResult::Error(os_error) => {
                self.result.store(-1, Ordering::SeqCst);
                self.error.store(if os_error.errno == 0 { -1 } else { os_error.errno }, Ordering::SeqCst);
            }
        }
    }
}

#[gen_stub_pymethods]
#[pymethods]
impl AIOOperation {
    #[staticmethod]
    #[pyo3(signature = (nbytes, fd, offset, priority=0))]
    fn read(nbytes: u64, fd: u32, offset: u64, priority: u16) -> PyResult<Self> {
        check_transfer_size(nbytes)?;
        Ok(AIOOperation::new_base(OpCode::Read, fd, offset, nbytes, None, priority))
    }

    #[staticmethod]
    #[pyo3(signature = (payload_bytes, fd, offset, priority=0))]
    fn write(
        #[gen_stub(override_type(type_repr = "bytes"))] payload_bytes: &Bound<'_, PyAny>,
        fd: u32, offset: u64, priority: u16,
    ) -> PyResult<Self> {
        let bytes = payload_bytes
            .cast::<PyBytes>()
            .map_err(|_| PyValueError::new_err("payload_bytes argument must be bytes"))?;
        check_transfer_size(bytes.as_bytes().len() as u64)?;
        Ok(AIOOperation::new_base(
            OpCode::Write,
            fd,
            offset,
            bytes.as_bytes().len() as u64,
            Some(bytes.clone().unbind()),
            priority,
        ))
    }

    #[staticmethod]
    #[pyo3(signature = (fd, priority=0))]
    fn fsync(fd: u32, priority: u16) -> Self {
        AIOOperation::new_base(OpCode::Fsync, fd, 0, 0, None, priority)
    }

    #[staticmethod]
    #[pyo3(signature = (fd, priority=0))]
    fn fdsync(fd: u32, priority: u16) -> Self {
        AIOOperation::new_base(OpCode::Fdsync, fd, 0, 0, None, priority)
    }

    #[gen_stub(override_return_type(type_repr = "bytes | int"))]
    fn get_value(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.check_not_in_flight()?;

        let error = self.error.load(Ordering::SeqCst);
        if error != 0 {
            let message = std::io::Error::from_raw_os_error(error).to_string();
            return Err(PySystemError::new_err(message));
        }

        match self.opcode {
            OpCode::Read => {
                let buf = self.result_buf.lock().unwrap();
                Ok(PyBytes::new(py, &buf).unbind().into())
            }
            OpCode::Write => {
                let result = self.result.load(Ordering::SeqCst);
                Ok(result.into_pyobject(py)?.into_any().unbind())
            }
            OpCode::Fsync | OpCode::Fdsync => Ok(py.None()),
        }
    }

    #[gen_stub(override_return_type(type_repr = "bool"))]
    fn set_callback(
        &self,
        #[gen_stub(override_type(type_repr = "collections.abc.Callable[[int], typing.Any]", imports = ("collections.abc", "typing")))]
        callback: &Bound<'_, PyAny>,
    ) -> PyResult<bool> {
        if !callback.is_callable() {
            return Err(PyValueError::new_err(format!(
                "object {} is not callable",
                callback.repr()?,
            )));
        }
        *self.callback.lock().unwrap() = Some(callback.clone().unbind());
        Ok(true)
    }

    #[getter]
    fn nbytes(&self) -> u64 {
        match self.opcode {
            // Dynamic: `requested_nbytes` until completion, then the
            // actual transferred count (may be less on a short read),
            // without needing `result_buf` allocated before completion.
            OpCode::Read => self.transferred_nbytes.load(Ordering::SeqCst),
            // Constant: the payload length, unaffected by completion.
            OpCode::Write | OpCode::Fsync | OpCode::Fdsync => self.requested_nbytes,
        }
    }

    #[getter]
    fn result(&self) -> i32 {
        self.result.load(Ordering::SeqCst)
    }

    #[getter]
    fn error(&self) -> i32 {
        self.error.load(Ordering::SeqCst)
    }

    #[getter]
    #[gen_stub(override_return_type(type_repr = "bytes | memoryview | None"))]
    fn payload(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.check_not_in_flight()?;

        match self.opcode {
            OpCode::Read => {
                let buf = self.result_buf.lock().unwrap();
                let bytes = PyBytes::new(py, &buf);
                let view = PyMemoryView::from(&bytes)?;
                Ok(view.unbind().into())
            }
            OpCode::Write => match &self.write_payload {
                Some(p) => Ok(p.clone_ref(py).into_any()),
                None => Ok(py.None()),
            },
            OpCode::Fsync | OpCode::Fdsync => Ok(py.None()),
        }
    }

    fn __repr__(&self) -> String {
        let mode = match self.opcode {
            OpCode::Read => "read",
            OpCode::Write => "write",
            OpCode::Fsync => "fsync",
            OpCode::Fdsync => "fdsync",
        };
        format!(
            "<Operation: mode=\"{}\", fd={}, offset={}, result={}>",
            mode,
            self.fileno,
            self.offset,
            self.result.load(Ordering::SeqCst),
        )
    }
}
