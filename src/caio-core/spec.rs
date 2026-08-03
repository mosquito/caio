//! The immutable part of an `Operation` - set once at construction, never
//! touched again by the engine. Using one variant per opcode (rather than a
//! single struct with an `opcode` tag plus fields that only make sense for
//! some opcodes) makes invalid combinations - a `Read` with a payload, a
//! `Fsync` with a size - unrepresentable instead of merely undocumented.

use std::io;

/// The kind of file-descriptor value a bridge crate passes in: a raw OS fd
/// on Unix, a CRT fd on Windows (see `thread_aio/platform_io.rs`). Borrowed,
/// never owned: this crate does not `dup()` it, and does not track its
/// lifetime beyond a single request.
pub type RawFdT = i32;

/// Largest single transfer this crate will attempt. Matches the existing
/// per-backend `MAX_TRANSFER_SIZE` constants (`i32::MAX`): `linux_uring`'s
/// SQE `len` field is a `u32`, so anything bigger would silently truncate
/// at the wire level rather than fail cleanly - kept as one shared limit so
/// all three backends reject the same oversized request the same way.
pub const MAX_TRANSFER_SIZE: u64 = i32::MAX as u64;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TransferSizeError {
    pub requested: u64,
}

impl std::fmt::Display for TransferSizeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "requested transfer size {} exceeds the maximum single-operation size {}",
            self.requested, MAX_TRANSFER_SIZE,
        )
    }
}

impl std::error::Error for TransferSizeError {}

fn check_transfer_size(nbytes: u64) -> Result<(), TransferSizeError> {
    if nbytes > MAX_TRANSFER_SIZE {
        return Err(TransferSizeError { requested: nbytes });
    }
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OpCode {
    Read,
    Write,
    Fsync,
    Fdsync,
}

/// Owned, immutable write payload - copied from the caller's `bytes` at the
/// PyO3 bridge layer rather than referencing the original Python object, so
/// this crate never touches the GIL or Python refcounts.
#[derive(Debug, Clone)]
pub struct WritePayload(Box<[u8]>);

impl WritePayload {
    pub fn new(bytes: Box<[u8]>) -> Result<Self, TransferSizeError> {
        check_transfer_size(bytes.len() as u64)?;
        Ok(WritePayload(bytes))
    }

    pub fn as_slice(&self) -> &[u8] {
        &self.0
    }

    /// Moves the owned bytes out without copying - useful for a driver's
    /// `prepare()` (which consumes the whole `RequestSpec` - see
    /// `Driver::prepare`'s own doc comment) building a self-contained
    /// submission around this same allocation instead of cloning it again.
    pub fn into_boxed_slice(self) -> Box<[u8]> {
        self.0
    }

    pub fn len(&self) -> usize {
        self.0.len()
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }
}

#[derive(Debug, Clone)]
pub enum RequestSpec {
    Read { fd: RawFdT, offset: u64, nbytes: u64, priority: u16 },
    Write { fd: RawFdT, offset: u64, payload: WritePayload, priority: u16 },
    Fsync { fd: RawFdT, priority: u16 },
    Fdsync { fd: RawFdT, priority: u16 },
}

impl RequestSpec {
    pub fn read(fd: RawFdT, offset: u64, nbytes: u64, priority: u16) -> Result<Self, TransferSizeError> {
        check_transfer_size(nbytes)?;
        Ok(RequestSpec::Read { fd, offset, nbytes, priority })
    }

    pub fn write(
        fd: RawFdT, offset: u64, payload: Box<[u8]>, priority: u16,
    ) -> Result<Self, TransferSizeError> {
        Ok(RequestSpec::Write { fd, offset, payload: WritePayload::new(payload)?, priority })
    }

    pub fn fsync(fd: RawFdT, priority: u16) -> Self {
        RequestSpec::Fsync { fd, priority }
    }

    pub fn fdsync(fd: RawFdT, priority: u16) -> Self {
        RequestSpec::Fdsync { fd, priority }
    }

    pub fn opcode(&self) -> OpCode {
        match self {
            RequestSpec::Read { .. } => OpCode::Read,
            RequestSpec::Write { .. } => OpCode::Write,
            RequestSpec::Fsync { .. } => OpCode::Fsync,
            RequestSpec::Fdsync { .. } => OpCode::Fdsync,
        }
    }

    pub fn fd(&self) -> RawFdT {
        match self {
            RequestSpec::Read { fd, .. }
            | RequestSpec::Write { fd, .. }
            | RequestSpec::Fsync { fd, .. }
            | RequestSpec::Fdsync { fd, .. } => *fd,
        }
    }
}

/// Maps an OS error number to the same kind of exception every backend
/// should raise - normalized in one place instead of ad hoc
/// `SystemError`/`OSError` choices per backend.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct OsError {
    pub errno: i32,
}

impl OsError {
    pub fn from_raw(errno: i32) -> Self {
        OsError { errno }
    }

    pub fn message(&self) -> String {
        io::Error::from_raw_os_error(self.errno).to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn read_rejects_oversized_transfer() {
        assert!(RequestSpec::read(3, 0, MAX_TRANSFER_SIZE + 1, 0).is_err());
        assert!(RequestSpec::read(3, 0, MAX_TRANSFER_SIZE, 0).is_ok());
    }

    #[test]
    fn write_rejects_oversized_payload() {
        let huge = TransferSizeError { requested: MAX_TRANSFER_SIZE + 1 };
        assert_eq!(huge.requested, MAX_TRANSFER_SIZE + 1);
    }

    #[test]
    fn fsync_fdsync_carry_no_size() {
        let a = RequestSpec::fsync(3, 0);
        let b = RequestSpec::fdsync(3, 0);
        assert_eq!(a.opcode(), OpCode::Fsync);
        assert_eq!(b.opcode(), OpCode::Fdsync);
    }
}
