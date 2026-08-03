//! The `Driver` trait - the only thing a backend (`thread`, `linux_aio`,
//! `io_uring`, or a test `FakeDriver`) has to implement. It decides *how*
//! to execute a request; it does not decide resubmit/callback/exception
//! semantics - those live in `Engine`/`StateMachine`, shared by all of them.

use crate::ids::RequestId;
use crate::spec::{OsError, RequestSpec};
use crate::completion::CompletionResult;

#[derive(Debug)]
pub struct PrepareError(pub String);

impl std::fmt::Display for PrepareError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl std::error::Error for PrepareError {}

/// A synchronous, per-request dispatch failure - e.g. `linux_aio`'s
/// `io_submit()` validates the fd/iocb eagerly and can reject a single
/// request outright (`EBADF`/`EINVAL`/`EFAULT`/`EAGAIN`), as opposed to a
/// capacity limit the engine already enforces before ever calling
/// `dispatch()`. Carries the raw `OsError` (not just a message) so a
/// bridge can map it to the typed exception matching that errno.
#[derive(Debug)]
pub struct DispatchError(pub OsError);

/// Reported by `Driver::poll()`. Split from `CompletionResult` because a
/// driver may need to tell the engine "this queued item is now actually
/// running" (thread: dequeued by a worker; io_uring: pushed to the kernel
/// by a flush) *before* it has anything resembling a result yet - that's
/// the `QUEUED -> SUBMITTED` transition, and it does not always happen
/// synchronously inside `dispatch()` (see `Driver::dispatch`'s own doc).
#[derive(Debug)]
pub enum DriverEvent {
    Submitted(RequestId),
    Completed(RequestId, CompletionResult),
}

/// One backend's actual execution mechanism. Every method here is meant to
/// be *thin*: it does not decide whether a resubmit is allowed, does not
/// invoke Python callbacks, and does not itself track outstanding count -
/// `Engine` owns all of that.
pub trait Driver {
    /// Backend-specific per-request submission metadata (a pinned `Iocb`
    /// for `linux_aio`, ring-queue bookkeeping for `io_uring`, a `Job` for
    /// the thread pool).
    type Submission;

    /// Whether this driver can attempt to cancel a request that has
    /// already reached `SUBMITTED` (as opposed to one still `QUEUED`,
    /// which every driver can drop before it starts). The thread backend
    /// cannot interrupt a blocking syscall already running and returns
    /// `false`; `linux_aio`/`io_uring` can always attempt an async cancel
    /// on a dispatched request and return `true`.
    fn supports_inflight_cancel(&self) -> bool;

    /// Fallible preparation step (the only fallible step in accepting a
    /// request - see `Engine::submit_many`'s transactional contract).
    /// Must not mutate any request's state nor consume ring/queue
    /// capacity; a failure here must leave the driver exactly as it was.
    ///
    /// Takes `spec` by value (not a reference): the engine's own registry
    /// entry does not keep a copy after this call (see `Engine`'s
    /// `InFlight` doc comment), so a driver building a self-contained
    /// `Submission` - e.g. copying a write payload's bytes alongside the
    /// fd/offset it needs to actually run the job - can move them
    /// straight out of `spec` instead of cloning.
    fn prepare(&mut self, spec: RequestSpec) -> Result<Self::Submission, PrepareError>;

    /// Hands an already-`prepare()`d submission to the kernel/pool.
    /// Capacity itself can never be the reason this fails - the engine's
    /// own guard (see `Engine::submit_many`'s transactional contract)
    /// never calls this beyond the capacity a driver was constructed
    /// with. But a per-request dispatch can still fail for reasons that
    /// have nothing to do with capacity - `linux_aio`'s `io_submit()`
    /// validates the fd/iocb eagerly and can reject a single request
    /// outright (`EBADF`/`EINVAL`/`EFAULT`) - so this returns `Result`,
    /// not a bare bool: `Err` rolls this one request back (never inserted
    /// into the registry) and stops the batch from accepting anything
    /// after it, exactly like a `prepare()` failure does.
    ///
    /// `Ok(true)` means the request is now actually `SUBMITTED`
    /// (dispatched synchronously, as `linux_aio`'s blocking `io_submit`
    /// or a fake driver might do); `Ok(false)` means it's still `QUEUED` -
    /// pending a later `DriverEvent::Submitted` from `poll()` (as
    /// `io_uring`'s stage-then-flush or the thread pool's
    /// queue-until-a-worker-is-free do).
    fn dispatch(&mut self, id: RequestId, submission: Self::Submission) -> Result<bool, DispatchError>;

    /// Best-effort attempt to cancel a `SUBMITTED` request. Only called
    /// when `supports_inflight_cancel()` is `true`. Does not itself
    /// resolve the target's outcome - the real completion (or a
    /// cancellation confirmation) still arrives through `poll()`.
    fn cancel_inflight(&mut self, id: RequestId);

    /// Drops an unstarted, still-`QUEUED` request (never reached the
    /// kernel/pool at all) - always possible, unlike `cancel_inflight`.
    fn cancel_queued(&mut self, id: RequestId);

    /// Non-blocking: returns whatever new events are currently available.
    fn poll(&mut self) -> Vec<DriverEvent>;

    /// Tears down native resources. Must not block indefinitely and must
    /// not invoke Python callbacks. Returns the IDs of requests it could
    /// not safely reap in time, if any.
    fn shutdown(&mut self) -> Vec<RequestId>;
}
