//! `caio-core` - the shared, safe Rust engine backing all three native
//! backends (design doc: `design/generalized-safe-design.md`). No PyO3, no
//! `unsafe`: this crate owns the `Operation` state machine, the outstanding
//! registry, capacity/backpressure, and the transactional submit path.
//! Backend-specific execution lives behind the `Driver` trait, implemented
//! separately per backend (and, for tests, by a `FakeDriver`).

#![deny(unsafe_code)]

pub mod completion;
pub mod driver;
pub mod ids;
pub mod registry;
pub mod spec;
pub mod state;

pub use completion::CompletionResult;
pub use driver::{Driver, DispatchError, DriverEvent, PrepareError};
pub use ids::{ContextId, RequestHandle, RequestId};
pub use registry::{Engine, EngineError, RejectReason, RejectedSubmission, SubmitReport};
pub use spec::{OpCode, OsError, RequestSpec, TransferSizeError, MAX_TRANSFER_SIZE};
pub use state::{CancelStatus, InvalidTransition, RequestState, StateMachine};
