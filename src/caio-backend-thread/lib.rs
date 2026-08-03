//! Pure-Rust `caio_core::Driver` for the thread-pool backend. No PyO3.

pub mod driver;
pub mod platform_io;
pub mod pool;

pub use driver::ThreadDriver;
pub use pool::{join_workers_with_deadline, DROP_REAP_TIMEOUT_SECS};
