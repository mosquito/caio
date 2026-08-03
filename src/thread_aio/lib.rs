use pyo3::prelude::*;

mod context;
mod operation;

#[pymodule]
fn thread_aio(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<context::AIOContext>()?;
    m.add_class::<operation::AIOOperation>()?;
    Ok(())
}

/// Gathers this crate's `#[gen_stub_pyclass]`/`#[gen_stub_pymethods]`-annotated
/// items into a `StubInfo` ready to write `caio/thread_aio.pyi`. Deliberately
/// not `pyo3_stub_gen::define_stub_info_gatherer!` (which reads
/// `pyproject.toml` from `CARGO_MANIFEST_DIR`): this repo's one
/// `pyproject.toml` is shared by all three native backends and can't tell
/// which one's stub_gen binary is asking - `StubInfo::from_project_root`
/// sidesteps that by taking the module name and `caio/` directory
/// explicitly. Lives here rather than in `bin/stub_gen.rs` because the
/// `inventory`-based registration `#[gen_stub_pyclass]` relies on needs the
/// gatherer co-located with the annotated items.
pub fn stub_info() -> pyo3_stub_gen::Result<pyo3_stub_gen::StubInfo> {
    let manifest_dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
    // manifest_dir is .../src/thread_aio; caio/ (the Python package
    // directory .pyi files belong in) is two levels up.
    let project_root = manifest_dir
        .parent()
        .and_then(std::path::Path::parent)
        .expect("src/thread_aio should be two levels below the repo root")
        .join("caio");
    pyo3_stub_gen::StubInfo::from_project_root(
        "thread_aio".to_string(),
        project_root,
        false, // pure Rust layout: one flat caio/thread_aio.pyi, not a package
        Default::default(),
    )
}
