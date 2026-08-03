//! Pure-Rust `caio_core::Driver` over raw io_uring. No PyO3. Linux-only,
//! enforced by whoever depends on this crate.

pub mod abi;
pub mod driver;

pub use driver::{UringDriver, UringSubmission, DROP_REAP_TIMEOUT_SECS};
