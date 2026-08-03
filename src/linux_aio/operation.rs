use std::sync::atomic::{AtomicBool, AtomicI32, AtomicI64, Ordering};
use std::sync::Mutex;

use pyo3::exceptions::{PyMemoryError, PyOverflowError, PyRuntimeError, PySystemError, PyValueError};
use pyo3::gc::PyVisit;
use pyo3::types::{PyBytes, PyMemoryView};
use pyo3::prelude::*;
use pyo3::PyTraverseError;
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

/// linux aio operation representation.
///
/// One-shot, same as `thread_aio::AIOOperation` - see that file's own doc
/// comment for the reasoning shared by both.
#[gen_stub_pyclass]
#[pyclass(name = "Operation", module = "linux_aio", weakref)]
pub struct AIOOperation {
    opcode: OpCode,
    #[pyo3(get)]
    fileno: u32,
    /// Kept as the original `i64` (matching this backend's own historical
    /// signature and the kernel's `aio_offset` field, unlike
    /// `thread_aio`/`linux_uring`'s `u64`) - converted to/from
    /// `RequestSpec`'s `u64` by simple bit-reinterpreting `as` casts,
    /// round-tripping exactly regardless of sign.
    offset: i64,
    priority: i16,
    requested_nbytes: u64,
    write_payload: Option<Py<PyBytes>>,

    result_buf: Mutex<Vec<u8>>,
    result_nbytes: AtomicI64,
    error: AtomicI32,
    callback: Mutex<Option<Py<PyAny>>>,
    /// Set once, in `Context::submit()`; cleared once this Operation
    /// reaches a terminal state (`apply_result()`). While set, it
    /// guarantees a Context is never torn down while an Operation
    /// submitted through it might still need it (e.g. `op.context.cancel(op)`).
    /// Clearing it on completion, rather than holding it until this
    /// Operation is itself deallocated, matters because the registry
    /// already holds its own strong reference for as long as a request is
    /// outstanding - a second, mutual strong reference back from Operation
    /// to Context is a genuine cycle (Context -> registry -> Operation ->
    /// context -> Context) that plain refcounting can't collect (PyO3
    /// classes don't participate in Python's cyclic GC by default).
    context: Mutex<Option<Py<PyAny>>>,
    /// True from acceptance until `apply_result` runs - gates
    /// `payload`/`get_value()` so they can't observe a buffer the kernel
    /// may still be writing into. Distinct from `request_id` below
    /// (which, once set, never goes back to `None`): this one does clear,
    /// but a one-shot `Operation` never gets a second chance to become
    /// `in_flight` again after it does.
    in_flight: AtomicBool,
    request_id: Mutex<Option<RequestHandle>>,
}

impl AIOOperation {
    fn new_base(
        opcode: OpCode, fileno: u32, offset: i64, priority: i16, requested_nbytes: u64,
        write_payload: Option<Py<PyBytes>>,
    ) -> Self {
        // Left empty until first submission, NOT eagerly zero-filled here:
        // `requested_nbytes` is caller-controlled (up to `MAX_TRANSFER_SIZE`,
        // just under 2 GiB), so a placeholder buffer here on top of the
        // driver's own separately-allocated read buffer would double the
        // memory an in-flight large read holds. `.nbytes`/`.get_value()`
        // read `result_nbytes`, never `result_buf.len()`, so this is safe
        // before completion too.
        let initial_buf = Vec::new();
        AIOOperation {
            opcode,
            fileno,
            offset,
            priority,
            requested_nbytes,
            write_payload,
            result_buf: Mutex::new(initial_buf),
            result_nbytes: AtomicI64::new(requested_nbytes as i64),
            error: AtomicI32::new(0),
            callback: Mutex::new(None),
            context: Mutex::new(None),
            in_flight: AtomicBool::new(false),
            request_id: Mutex::new(None),
        }
    }

    pub(crate) fn already_submitted(&self) -> bool {
        self.request_id.lock().unwrap().is_some()
    }

    pub(crate) fn request_id(&self) -> Option<RequestHandle> {
        *self.request_id.lock().unwrap()
    }

    /// Builds a fresh `RequestSpec` from this operation's own stored
    /// fields - see `thread_aio::AIOOperation::build_spec`'s doc comment
    /// for why this never consumes anything, even though the resulting
    /// `Operation` itself is one-shot.
    pub(crate) fn build_spec(&self, py: Python<'_>) -> PyResult<RequestSpec> {
        let spec = match self.opcode {
            OpCode::Read => {
                RequestSpec::read(self.fileno as i32, self.offset as u64, self.requested_nbytes, self.priority as u16)
            }
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
                RequestSpec::write(self.fileno as i32, self.offset as u64, payload.into_boxed_slice(), self.priority as u16)
            }
            OpCode::Fsync => Ok(RequestSpec::fsync(self.fileno as i32, self.priority as u16)),
            OpCode::Fdsync => Ok(RequestSpec::fdsync(self.fileno as i32, self.priority as u16)),
        };
        spec.map_err(|e| PyOverflowError::new_err(e.to_string()))
    }

    pub(crate) fn mark_submitted(&self, handle: RequestHandle) {
        *self.request_id.lock().unwrap() = Some(handle);
        self.in_flight.store(true, Ordering::SeqCst);
    }

    pub(crate) fn set_context(&self, ctx: &Bound<'_, PyAny>) {
        *self.context.lock().unwrap() = Some(ctx.clone().unbind());
    }

    pub(crate) fn take_callback(&self) -> Option<Py<PyAny>> {
        self.callback.lock().unwrap().take()
    }

    pub(crate) fn result_value(&self) -> i64 {
        self.result_nbytes.load(Ordering::SeqCst).max(0)
    }

    fn check_not_in_flight(&self) -> PyResult<()> {
        if self.in_flight.load(Ordering::SeqCst) {
            return Err(PyRuntimeError::new_err(
                "operation is still in flight; wait for completion before accessing payload/get_value",
            ));
        }
        Ok(())
    }

    pub(crate) fn apply_result(&self, result: CompletionResult) {
        self.in_flight.store(false, Ordering::SeqCst);
        // Breaks the Context<->Operation reference cycle right as this
        // Operation reaches a terminal state - see the `context` field's
        // own doc comment for why holding it any longer would leak both
        // objects (and the kernel resources behind them).
        self.context.lock().unwrap().take();
        match result {
            CompletionResult::Read { buffer, transferred } => {
                *self.result_buf.lock().unwrap() = buffer.into_vec();
                self.result_nbytes.store(transferred as i64, Ordering::SeqCst);
                self.error.store(0, Ordering::SeqCst);
            }
            CompletionResult::Write { transferred } => {
                self.result_nbytes.store(transferred as i64, Ordering::SeqCst);
                self.error.store(0, Ordering::SeqCst);
            }
            CompletionResult::Sync => {
                self.result_nbytes.store(0, Ordering::SeqCst);
                self.error.store(0, Ordering::SeqCst);
            }
            CompletionResult::Cancelled => {
                self.error.store(-1, Ordering::SeqCst);
            }
            CompletionResult::Error(os_error) => {
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
    fn read(nbytes: u64, fd: u32, offset: i64, priority: i16) -> PyResult<Self> {
        check_transfer_size(nbytes)?;
        Ok(AIOOperation::new_base(OpCode::Read, fd, offset, priority, nbytes, None))
    }

    #[staticmethod]
    #[pyo3(signature = (payload_bytes, fd, offset, priority=0))]
    fn write(
        #[gen_stub(override_type(type_repr = "bytes"))] payload_bytes: &Bound<'_, PyAny>,
        fd: u32, offset: i64, priority: i16,
    ) -> PyResult<Self> {
        let bytes = payload_bytes
            .cast::<PyBytes>()
            .map_err(|_| PyValueError::new_err("payload_bytes argument must be bytes"))?;
        check_transfer_size(bytes.as_bytes().len() as u64)?;
        Ok(AIOOperation::new_base(
            OpCode::Write,
            fd,
            offset,
            priority,
            bytes.as_bytes().len() as u64,
            Some(bytes.clone().unbind()),
        ))
    }

    #[staticmethod]
    #[pyo3(signature = (fd, priority=0))]
    fn fsync(fd: u32, priority: i16) -> Self {
        AIOOperation::new_base(OpCode::Fsync, fd, 0, priority, 0, None)
    }

    #[staticmethod]
    #[pyo3(signature = (fd, priority=0))]
    fn fdsync(fd: u32, priority: i16) -> Self {
        AIOOperation::new_base(OpCode::Fdsync, fd, 0, priority, 0, None)
    }

    #[gen_stub(override_return_type(type_repr = "bytes | int"))]
    fn get_value(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.check_not_in_flight()?;

        let error = self.error.load(Ordering::SeqCst);
        if error != 0 {
            let message = std::io::Error::from_raw_os_error(error).to_string();
            return Err(PySystemError::new_err(message));
        }

        let nbytes = self.result_nbytes.load(Ordering::SeqCst).max(0) as usize;

        match self.opcode {
            OpCode::Read => {
                let buf = self.result_buf.lock().unwrap();
                let len = nbytes.min(buf.len());
                Ok(PyBytes::new(py, &buf[..len]).unbind().into())
            }
            OpCode::Write => Ok((nbytes as u64).into_pyobject(py)?.into_any().unbind()),
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
    #[gen_stub(override_return_type(type_repr = "Context | None"))]
    fn context(&self, py: Python<'_>) -> Py<PyAny> {
        match &*self.context.lock().unwrap() {
            Some(ctx) => ctx.clone_ref(py),
            None => py.None(),
        }
    }

    #[getter]
    fn offset(&self) -> i64 {
        self.offset
    }

    #[getter]
    fn priority(&self) -> i16 {
        self.priority
    }

    #[getter]
    fn nbytes(&self) -> u64 {
        self.result_nbytes.load(Ordering::SeqCst).max(0) as u64
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
        format!("<Operation: mode=\"{}\", fd={}, offset={}>", mode, self.fileno, self.offset)
    }

    // CPython GC slot methods (must live in this same `#[pymethods]` block -
    // PyO3 rejects a second one per pyclass without `multiple-pymethods`;
    // `#[gen_stub(skip)]` is required since `PyVisit` doesn't implement
    // pyo3-stub-gen's `PyStubType`). Breaks the Context<->Operation
    // reference cycle (see `context` field's doc comment) when a request
    // is abandoned without ever calling `process_events()`/`poll()` -
    // `apply_result()` already breaks it on normal completion.
    #[gen_stub(skip)]
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(payload) = self.write_payload.as_ref() {
            visit.call(payload)?;
        }
        if let Ok(guard) = self.callback.lock() {
            if let Some(cb) = guard.as_ref() {
                visit.call(cb)?;
            }
        }
        if let Ok(guard) = self.context.lock() {
            if let Some(ctx) = guard.as_ref() {
                visit.call(ctx)?;
            }
        }
        Ok(())
    }

    #[gen_stub(skip)]
    fn __clear__(&mut self) {
        self.write_payload = None;
        *self.callback.get_mut().unwrap() = None;
        *self.context.get_mut().unwrap() = None;
    }
}

pub(crate) fn io_submit_error(errno: i32) -> PyErr {
    match errno {
        libc::EAGAIN => PyOverflowError::new_err(
            "Insufficient resources are available to queue any iocbs [EAGAIN]",
        ),
        libc::EBADF => PyValueError::new_err(
            "The file descriptor specified in the first iocb is invalid [EBADF]",
        ),
        libc::EFAULT => PyValueError::new_err(
            "One of the data structures points to invalid data [EFAULT]",
        ),
        libc::EINVAL => PyValueError::new_err(
            "The AIO context specified by ctx_id is invalid. nr is less than 0. \
             The iocb at *iocbpp[0] is not properly initialized, the operation \
             specified is invalid for the file descriptor in the iocb, or the \
             value in the aio_reqprio field is invalid. [EINVAL]",
        ),
        _ => PySystemError::new_err(std::io::Error::from_raw_os_error(errno).to_string()),
    }
}
