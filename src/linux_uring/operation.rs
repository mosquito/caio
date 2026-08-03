use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU64, Ordering};
use std::sync::Mutex;

use pyo3::exceptions::{PyMemoryError, PyOverflowError, PyRuntimeError, PySystemError, PyValueError};
use pyo3::types::PyBytes;
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

/// io_uring operation representation.
///
/// One-shot, same as `thread_aio`/`linux_aio`'s `AIOOperation` -
/// resubmitting the same completed object is rejected. The read buffer is
/// a Rust-owned `Box<[u8]>` allocated once, in `prepare()` at actual
/// submission time, and lives and dies with this one `Operation`.
#[gen_stub_pyclass]
#[pyclass(name = "Operation", module = "linux_uring", weakref)]
pub struct AIOOperation {
    opcode: OpCode,
    #[pyo3(get)]
    fileno: u32,
    #[pyo3(get)]
    offset: u64,
    requested_nbytes: u64,
    write_payload: Option<Py<PyBytes>>,

    result_buf: Mutex<Vec<u8>>,
    /// Read: requested size until completion, then the actual transfer
    /// count (may be less on a short read) - reported by the `nbytes`
    /// getter and used to slice `result_buf` in `get_value()`. Kept
    /// separate from `result_buf`'s own length, which stays at the full
    /// requested size even after a short read (`.payload` exposes that
    /// whole, zero-tailed buffer - see its own doc comment). Write/Fsync/
    /// Fdsync: unused, `nbytes` reports `requested_nbytes` directly.
    transferred_nbytes: AtomicU64,
    result: AtomicI32,
    error: AtomicI32,
    callback: Mutex<Option<Py<PyAny>>>,
    request_id: Mutex<Option<RequestHandle>>,
    /// True from acceptance until `apply_result` runs - gates
    /// `payload`/`get_value()` so they can't observe a buffer the kernel
    /// may still be writing into (`result_buf` is zero-copy - the kernel
    /// writes straight into it, not a Rust-side copy). Distinct from
    /// `request_id` (which, once set, never goes back to `None`): this
    /// one clears on completion, but a one-shot `Operation` never gets a
    /// second chance to become `in_flight` again afterward.
    in_flight: AtomicBool,
}

impl AIOOperation {
    fn new_base(opcode: OpCode, fileno: u32, offset: u64, requested_nbytes: u64, write_payload: Option<Py<PyBytes>>) -> Self {
        // Left empty until first submission, NOT eagerly zero-filled here:
        // the actual, possibly huge (up to MAX_TRANSFER_SIZE) read buffer
        // is allocated fallibly in `caio-backend-uring`'s `prepare()` (via
        // `try_reserve_exact`) at real submission time - allocating it
        // here instead would make `Operation.read(n, ...)` itself capable
        // of aborting the whole process on OOM, rather than raising a
        // catchable exception from `submit()`.
        let initial_buf = Vec::new();
        AIOOperation {
            opcode,
            fileno,
            offset,
            requested_nbytes,
            write_payload,
            result_buf: Mutex::new(initial_buf),
            transferred_nbytes: AtomicU64::new(requested_nbytes),
            result: AtomicI32::new(0),
            error: AtomicI32::new(0),
            callback: Mutex::new(None),
            request_id: Mutex::new(None),
            in_flight: AtomicBool::new(false),
        }
    }

    pub(crate) fn already_submitted(&self) -> bool {
        self.request_id.lock().unwrap().is_some()
    }

    pub(crate) fn request_id(&self) -> Option<RequestHandle> {
        *self.request_id.lock().unwrap()
    }

    pub(crate) fn build_spec(&self, py: Python<'_>) -> PyResult<RequestSpec> {
        let spec = match self.opcode {
            OpCode::Read => RequestSpec::read(self.fileno as i32, self.offset, self.requested_nbytes, 0),
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
                RequestSpec::write(self.fileno as i32, self.offset, payload.into_boxed_slice(), 0)
            }
            OpCode::Fsync => Ok(RequestSpec::fsync(self.fileno as i32, 0)),
            OpCode::Fdsync => Ok(RequestSpec::fdsync(self.fileno as i32, 0)),
        };
        spec.map_err(|e| PyOverflowError::new_err(e.to_string()))
    }

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
        match result {
            CompletionResult::Read { buffer, transferred } => {
                // Full, originally-allocated buffer kept as-is (zeroed
                // tail past a short read intact) - `.payload` exposes this
                // whole thing; only `get_value()`/`nbytes` slice it down
                // to `transferred`.
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
    fn read(nbytes: u64, fd: u32, offset: u64, priority: u16) -> PyResult<Self> {
        let _ = priority; // discarded - unused by this backend
        check_transfer_size(nbytes)?;
        Ok(AIOOperation::new_base(OpCode::Read, fd, offset, nbytes, None))
    }

    #[staticmethod]
    #[pyo3(signature = (payload_bytes, fd, offset, priority=0))]
    fn write(
        #[gen_stub(override_type(type_repr = "bytes"))] payload_bytes: &Bound<'_, PyAny>,
        fd: u32, offset: u64, priority: u16,
    ) -> PyResult<Self> {
        let _ = priority;
        let bytes = payload_bytes
            .cast::<PyBytes>()
            .map_err(|_| PyValueError::new_err("payload_bytes must be bytes"))?;
        let nbytes = bytes.as_bytes().len() as u64;
        check_transfer_size(nbytes)?;
        Ok(AIOOperation::new_base(OpCode::Write, fd, offset, nbytes, Some(bytes.clone().unbind())))
    }

    #[staticmethod]
    #[pyo3(signature = (fd, priority=0))]
    fn fsync(fd: u32, priority: u16) -> Self {
        let _ = priority;
        AIOOperation::new_base(OpCode::Fsync, fd, 0, 0, None)
    }

    #[staticmethod]
    #[pyo3(signature = (fd, priority=0))]
    fn fdsync(fd: u32, priority: u16) -> Self {
        let _ = priority;
        AIOOperation::new_base(OpCode::Fdsync, fd, 0, 0, None)
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
                let transferred = (self.transferred_nbytes.load(Ordering::SeqCst) as usize).min(buf.len());
                Ok(PyBytes::new(py, &buf[..transferred]).unbind().into())
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
            OpCode::Read => self.transferred_nbytes.load(Ordering::SeqCst),
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
    #[gen_stub(override_return_type(type_repr = "bytes | None"))]
    fn payload(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.check_not_in_flight()?;

        Ok(match self.opcode {
            OpCode::Read => {
                let buf = self.result_buf.lock().unwrap();
                PyBytes::new(py, &buf).unbind().into()
            }
            OpCode::Write => match &self.write_payload {
                Some(p) => p.clone_ref(py).into_any(),
                None => py.None(),
            },
            OpCode::Fsync | OpCode::Fdsync => py.None(),
        })
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
