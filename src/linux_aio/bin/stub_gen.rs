//! Regenerates `caio/linux_aio.pyi` - see `thread_aio/bin/stub_gen.rs`'s own
//! doc comment; same pattern, one per backend.

fn main() -> pyo3_stub_gen::Result<()> {
    linux_aio::stub_info()?.generate()
}
