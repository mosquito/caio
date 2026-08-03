//! Pure-Rust `caio_core::Driver` over raw Linux AIO. No PyO3. Linux-only
//! (the syscalls this wraps don't exist elsewhere); enforced by whoever
//! depends on this crate, not by a `cfg` here.

pub mod abi;
pub mod driver;

pub use driver::LinuxAioDriver;
