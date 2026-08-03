use pyo3::exceptions::PyImportError;
use pyo3::prelude::*;

use caio_backend_uring::abi;

mod context;
mod operation;

/// Requires Linux 5.6+ (IORING_OP_READ / IORING_OP_WRITE without iovec).
fn check_kernel_support() -> PyResult<()> {
    let mut uts: libc::utsname = unsafe { std::mem::zeroed() };
    if unsafe { libc::uname(&mut uts) } != 0 {
        return Err(PyImportError::new_err("uname() failed"));
    }

    let release = unsafe { std::ffi::CStr::from_ptr(uts.release.as_ptr()) }
        .to_string_lossy()
        .into_owned();

    let mut parts = release.splitn(3, '.');
    let major: u32 = parts.next().unwrap_or("0").parse().unwrap_or(0);
    let minor: u32 = parts.next().unwrap_or("0").parse().unwrap_or(0);

    if !(major > 5 || (major == 5 && minor >= 6)) {
        return Err(PyImportError::new_err(format!(
            "linux_uring requires Linux 5.6+ (IORING_OP_READ/WRITE), current kernel is {}",
            release,
        )));
    }

    // Quick probe: ensure the io_uring syscall is actually usable (e.g. not
    // blocked by a seccomp profile - the default Docker/Podman/OrbStack
    // container profile blocks it, though a real kernel version check
    // above should catch anything older).
    let mut probe = abi::IoUringParams::zeroed();
    match abi::io_uring_setup(1, &mut probe) {
        Ok(fd) => {
            unsafe { libc::close(fd) };
            Ok(())
        }
        Err(e) if e.raw_os_error() == Some(libc::ENOSYS) => Err(PyImportError::new_err(format!(
            "io_uring_setup failed: syscall not available (ENOSYS). This usually means a \
             seccomp filter is blocking io_uring. To fix:\n\
             \x20 Docker/Podman : add --security-opt seccomp=unconfined\n\
             \x20 Kubernetes    : set securityContext.seccompProfile.type=Unconfined\n\
             \x20 OrbStack      : use a plain Linux VM instead of a container context\n\
             Kernel: {}",
            release,
        ))),
        Err(e) => Err(PyImportError::new_err(format!("io_uring_setup probe failed: {}", e))),
    }
}

#[pymodule]
fn linux_uring(m: &Bound<'_, PyModule>) -> PyResult<()> {
    check_kernel_support()?;
    m.add_class::<context::AIOContext>()?;
    m.add_class::<operation::AIOOperation>()?;
    Ok(())
}

/// Gathers this crate's `#[gen_stub_pyclass]`/`#[gen_stub_pymethods]`-annotated
/// items into a `StubInfo` ready to write `caio/linux_uring.pyi` - see
/// `thread_aio/lib.rs::stub_info`'s own doc comment for why this uses
/// `StubInfo::from_project_root` directly instead of the
/// `define_stub_info_gatherer!` macro.
pub fn stub_info() -> pyo3_stub_gen::Result<pyo3_stub_gen::StubInfo> {
    let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    let project_root = manifest_dir
        .parent()
        .and_then(std::path::Path::parent)
        .expect("src/linux_uring should be two levels below the repo root")
        .join("caio");
    pyo3_stub_gen::StubInfo::from_project_root(
        "linux_uring".to_string(),
        project_root,
        false,
        Default::default(),
    )
}
