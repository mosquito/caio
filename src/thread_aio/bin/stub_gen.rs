//! Regenerates `caio/thread_aio.pyi` from this crate's own
//! `#[gen_stub_pyclass]`/`#[gen_stub_pymethods]`-annotated types. Run
//! `cargo run --bin stub_gen --manifest-path src/thread_aio/Cargo.toml`
//! whenever `Context`/`Operation`'s public Python API changes - not part of
//! the normal build (the `.pyi` is checked into git like any other source
//! file), so nothing here runs automatically on `uv sync`/`maturin build`.

fn main() -> pyo3_stub_gen::Result<()> {
    thread_aio::stub_info()?.generate()
}
