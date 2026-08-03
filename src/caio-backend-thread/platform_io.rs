//! Positioned (pread/pwrite-style) file I/O that works the same way on both
//! Unix and Windows, operating on a raw OS-level file descriptor without
//! taking ownership of it (the Python side owns the fd's lifecycle via
//! os.close()).

use std::io;

/// The kind of file-descriptor value the Python side passes us: on Unix this
/// is a raw fd, on Windows it's a CRT fd (from `os.open`), NOT a raw HANDLE.
pub type RawFdT = i32;

#[cfg(unix)]
fn check(result: libc::ssize_t) -> io::Result<usize> {
    if result < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(result as usize)
    }
}

/// By default the MSVC CRT's response to an invalid parameter (e.g.
/// `_get_osfhandle` given a closed/bogus fd) is to invoke an "invalid
/// parameter handler" that calls `abort()`, killing the whole process
/// instead of just failing the one call. Installing a no-op handler makes
/// it behave like a normal libc call instead: set errno/`GetLastError` and
/// return -1, which the existing error path below already handles.
///
/// Uses the thread-local `_set_thread_local_invalid_parameter_handler`,
/// not the process-wide one - the process-wide version would silently
/// overwrite whatever handler any other code in the process (another
/// CPython extension, the process's own error handling) had installed,
/// for the process's whole lifetime. `Guard` restores whatever the
/// calling thread had before as soon as it's dropped.
#[cfg(windows)]
mod invalid_parameter_guard {
    #[allow(non_camel_case_types)]
    type Handler = unsafe extern "C" fn(*const u16, *const u16, *const u16, u32, usize);

    extern "C" {
        fn _set_thread_local_invalid_parameter_handler(new_handler: Option<Handler>) -> Option<Handler>;
    }

    unsafe extern "C" fn noop(
        _expression: *const u16,
        _function: *const u16,
        _file: *const u16,
        _line: u32,
        _reserved: usize,
    ) {
    }

    pub struct Guard {
        previous: Option<Handler>,
    }

    impl Guard {
        pub fn install() -> Self {
            let previous = unsafe { _set_thread_local_invalid_parameter_handler(Some(noop)) };
            Guard { previous }
        }
    }

    impl Drop for Guard {
        fn drop(&mut self) {
            unsafe {
                _set_thread_local_invalid_parameter_handler(self.previous);
            }
        }
    }
}

#[cfg(windows)]
fn with_file<R>(fd: RawFdT, f: impl FnOnce(&std::fs::File) -> io::Result<R>) -> io::Result<R> {
    use std::fs::File;
    use std::mem::ManuallyDrop;
    use std::os::windows::io::{FromRawHandle, RawHandle};

    // `os.open()`/`fp.fileno()` on Windows returns a CRT fd, not a HANDLE.
    // `_get_osfhandle` recovers the underlying HANDLE. Scoped to just this
    // call, not `f`'s own body below - restored via `Guard`'s `Drop` the
    // moment `_get_osfhandle` returns.
    let handle = {
        let _guard = invalid_parameter_guard::Guard::install();
        unsafe { libc::get_osfhandle(fd) }
    };
    if handle == -1isize as libc::intptr_t {
        return Err(io::Error::last_os_error());
    }

    // SAFETY: `handle` is a valid HANDLE borrowed from the CRT fd for the
    // lifetime of this call. ManuallyDrop prevents `File`'s Drop impl from
    // closing it out from under the Python side.
    let file = unsafe { File::from_raw_handle(handle as RawHandle) };
    let file = ManuallyDrop::new(file);
    f(&file)
}

// Unix: raw pread(2)/pwrite(2)/fsync(2)/fdatasync(2) calls directly against
// the borrowed fd, never constructing a `std::fs::File` from it at all.
// `File::from_raw_fd` is documented as requiring the fd be "open, and
// suitable for assuming ownership" - a borrowed fd we don't own and won't
// close satisfies neither part of that contract, even wrapped in
// `ManuallyDrop` to skip the actual close(). Going straight to libc sidesteps
// the question entirely instead of relying on `File`'s methods never
// happening to depend on owning it (true today, not a guarantee).

#[cfg(unix)]
pub fn pread(fd: RawFdT, buf: &mut [u8], offset: u64) -> io::Result<usize> {
    check(unsafe {
        libc::pread(fd, buf.as_mut_ptr() as *mut libc::c_void, buf.len(), offset as libc::off_t)
    })
}

#[cfg(windows)]
pub fn pread(fd: RawFdT, buf: &mut [u8], offset: u64) -> io::Result<usize> {
    use std::os::windows::fs::FileExt;
    // Despite the name, `seek_read` takes `&self` (not `&mut self`) and is
    // implemented via ReadFile+OVERLAPPED with an explicit offset - it does
    // NOT move a shared file cursor, so it's safe to call concurrently from
    // multiple threads, just like pread.
    with_file(fd, |file| file.seek_read(buf, offset))
}

#[cfg(unix)]
pub fn pwrite(fd: RawFdT, buf: &[u8], offset: u64) -> io::Result<usize> {
    check(unsafe {
        libc::pwrite(fd, buf.as_ptr() as *const libc::c_void, buf.len(), offset as libc::off_t)
    })
}

#[cfg(windows)]
pub fn pwrite(fd: RawFdT, buf: &[u8], offset: u64) -> io::Result<usize> {
    use std::os::windows::fs::FileExt;
    // Same non-cursor-moving, concurrency-safe caveat as `seek_read` above.
    with_file(fd, |file| file.seek_write(buf, offset))
}

#[cfg(unix)]
pub fn fsync(fd: RawFdT) -> io::Result<()> {
    check(unsafe { libc::fsync(fd) } as libc::ssize_t).map(|_| ())
}

#[cfg(windows)]
pub fn fsync(fd: RawFdT) -> io::Result<()> {
    with_file(fd, |file| file.sync_all())
}

#[cfg(all(unix, target_os = "linux"))]
pub fn fdatasync(fd: RawFdT) -> io::Result<()> {
    check(unsafe { libc::fdatasync(fd) } as libc::ssize_t).map(|_| ())
}

#[cfg(all(unix, not(target_os = "linux")))]
pub fn fdatasync(fd: RawFdT) -> io::Result<()> {
    // fdatasync(2) doesn't exist on macOS (or most non-Linux Unixes) -
    // fall back to a full fsync, matching what `std::fs::File::sync_data()`
    // itself does on these platforms (and what the original C
    // implementation's own `#ifdef HAVE_FDATASYNC ... #else fsync #endif`
    // fallback did).
    fsync(fd)
}

#[cfg(windows)]
pub fn fdatasync(fd: RawFdT) -> io::Result<()> {
    // `File::sync_data` maps to fdatasync on platforms that have it and
    // falls back to a full sync elsewhere - matching the C code's own
    // `#ifdef HAVE_FDATASYNC ... #else fsync #endif` fallback.
    with_file(fd, |file| file.sync_data())
}
