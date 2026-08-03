use pyo3::exceptions::PyImportError;
use pyo3::prelude::*;

use caio_backend_linux_aio::abi;

mod context;
mod operation;

fn check_kernel_support() -> PyResult<()> {
    let mut uts: libc::utsname = unsafe { std::mem::zeroed() };
    if unsafe { libc::uname(&mut uts) } != 0 {
        return Err(PyImportError::new_err("Can not detect linux kernel version"));
    }

    let release = unsafe { std::ffi::CStr::from_ptr(uts.release.as_ptr()) }
        .to_string_lossy()
        .into_owned();

    let mut parts = release.splitn(3, '.');
    let major: u32 = parts.next().unwrap_or("0").parse().unwrap_or(0);
    let minor: u32 = parts.next().unwrap_or("0").parse().unwrap_or(0);

    let supported = major > 4 || (major == 4 && minor >= 18);
    if !supported {
        return Err(PyImportError::new_err(format!(
            "Linux kernel supported since 4.18 but current kernel is {}.",
            release,
        )));
    }

    // Probe io_setup/io_destroy at import time, so e.g. a container without
    // AIO enabled fails at import rather than at first use.
    match abi::io_setup(1) {
        Ok(ctx) => {
            abi::io_destroy(ctx).map_err(|e| {
                PyImportError::new_err(format!("Error on io_destroy with code {}", e))
            })?;
        }
        Err(e) => {
            return Err(PyImportError::new_err(format!("Error on io_setup with code {}", e)));
        }
    }

    Ok(())
}

#[pymodule]
fn linux_aio(m: &Bound<'_, PyModule>) -> PyResult<()> {
    check_kernel_support()?;
    m.add_class::<context::AIOContext>()?;
    m.add_class::<operation::AIOOperation>()?;
    Ok(())
}

/// Gathers this crate's `#[gen_stub_pyclass]`/`#[gen_stub_pymethods]`-annotated
/// items into a `StubInfo` ready to write `caio/linux_aio.pyi` - see
/// `thread_aio/lib.rs::stub_info`'s own doc comment for why this uses
/// `StubInfo::from_project_root` directly instead of the
/// `define_stub_info_gatherer!` macro.
pub fn stub_info() -> pyo3_stub_gen::Result<pyo3_stub_gen::StubInfo> {
    let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    let project_root = manifest_dir
        .parent()
        .and_then(std::path::Path::parent)
        .expect("src/linux_aio should be two levels below the repo root")
        .join("caio");
    pyo3_stub_gen::StubInfo::from_project_root(
        "linux_aio".to_string(),
        project_root,
        false,
        Default::default(),
    )
}
