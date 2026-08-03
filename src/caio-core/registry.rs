//! `Engine<D>` - the single source of truth for outstanding requests,
//! shared by every backend. Owns the registry, the capacity guard, and the
//! transactional submit path; delegates only actual execution to `D:
//! Driver`.

use std::collections::HashMap;

use crate::driver::{Driver, DriverEvent, DispatchError, PrepareError};
use crate::ids::{ContextId, IdSequence, RequestHandle, RequestId};
use crate::spec::{OsError, RequestSpec};
use crate::state::{CancelStatus, InvalidTransition, RequestState, StateMachine};
use crate::completion::CompletionResult;

/// Once `Driver::prepare()`/`dispatch()` are called the driver itself owns
/// whatever backend-specific submission state it needs (its own internal
/// map keyed by `RequestId`, matching how e.g. `linux_aio` would keep a
/// pinned `Iocb`) - `prepare()` consumes the `RequestSpec` by value (see
/// its own doc comment), so the engine's registry entry does not keep a
/// copy either; it only holds what *it* is responsible for, the state
/// machine.
struct InFlight {
    state: StateMachine,
}

/// Design doc's Shutdown contract: `OPEN -> CLOSING -> CLOSED`. Once
/// `CLOSING`, no new submissions are accepted (see `submit_many`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ContextState {
    Open,
    Closing,
    Closed,
}

#[derive(Debug, PartialEq, Eq)]
pub enum RejectReason {
    /// The ring/queue was already full when this item's turn came.
    CapacityExceeded,
    /// `Driver::prepare()` itself failed (e.g. an allocation failure).
    PrepareFailed(String),
    /// `Driver::dispatch()` itself failed for this specific request (e.g.
    /// `linux_aio`'s `io_submit()` rejecting a bad fd) - distinct from
    /// `CapacityExceeded`, which the engine already rules out before ever
    /// calling `dispatch()`. Carries the raw errno so a bridge can raise
    /// the typed exception matching it.
    DispatchFailed(OsError),
    /// Not attempted because an earlier item in the same batch already hit
    /// one of the failures above - the batch stops accepting further items
    /// the moment something goes wrong.
    NotAttempted,
    /// The Context is `CLOSING`/`CLOSED` - see `begin_close()`.
    ContextClosed,
}

#[derive(Debug)]
pub struct RejectedSubmission {
    pub index: usize,
    pub reason: RejectReason,
}

#[derive(Debug, Default)]
pub struct SubmitReport {
    pub accepted: Vec<RequestHandle>,
    pub rejected: Vec<RejectedSubmission>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EngineError {
    UnknownRequest,
    WrongContext,
    InvalidTransition,
}

impl From<InvalidTransition> for EngineError {
    fn from(_: InvalidTransition) -> Self {
        EngineError::InvalidTransition
    }
}

pub struct Engine<D: Driver> {
    context_id: ContextId,
    ids: IdSequence,
    capacity: usize,
    driver: D,
    outstanding: HashMap<RequestId, InFlight>,
    context_state: ContextState,
}

impl<D: Driver> Engine<D> {
    pub fn new(context_id: ContextId, capacity: usize, driver: D) -> Self {
        Engine {
            context_id,
            ids: IdSequence::default(),
            capacity,
            driver,
            outstanding: HashMap::new(),
            context_state: ContextState::Open,
        }
    }

    pub fn context_state(&self) -> ContextState {
        self.context_state
    }

    pub fn context_id(&self) -> ContextId {
        self.context_id
    }

    /// Escape hatch for backend-specific teardown that a generic `Driver`
    /// method can't express - see `caio_backend_thread::ThreadDriver`'s own
    /// `signal_shutdown()`/`take_workers()` doc comments for the concrete
    /// case this exists for (a PyO3 bridge must not hold this `Engine`
    /// behind a lock across a blocking thread-join, since a worker thread
    /// may need that same lock to deliver its own last completion before
    /// it can exit).
    pub fn driver_mut(&mut self) -> &mut D {
        &mut self.driver
    }

    pub fn outstanding_count(&self) -> usize {
        self.outstanding.len()
    }

    pub fn state_of(&self, id: RequestId) -> Option<RequestState> {
        self.outstanding.get(&id).map(|inflight| inflight.state.state())
    }

    /// Submits a whole batch transactionally: every accepted item's
    /// `prepare()` succeeds and its capacity slot is reserved *before* any
    /// of them is committed (marked `QUEUED`, dispatched, or given a
    /// `RequestId`). A prepare failure or capacity limit stops accepting
    /// *further* items but never undoes ones already prepared before it -
    /// those still commit normally.
    pub fn submit_many(&mut self, specs: Vec<RequestSpec>) -> SubmitReport {
        if self.context_state != ContextState::Open {
            let rejected = (0..specs.len())
                .map(|index| RejectedSubmission { index, reason: RejectReason::ContextClosed })
                .collect();
            return SubmitReport { accepted: Vec::new(), rejected };
        }

        // Pass 1 (fallible): capacity check + prepare() for as much of the
        // batch as keeps succeeding. Original index travels with each
        // surviving item since dispatch() (pass 2) can also fail
        // per-request and needs to report against the same position.
        let mut prepared: Vec<(usize, D::Submission)> = Vec::with_capacity(specs.len());
        let mut rejected: Vec<RejectedSubmission> = Vec::new();
        let mut stop = false;

        for (index, spec) in specs.into_iter().enumerate() {
            if stop {
                rejected.push(RejectedSubmission { index, reason: RejectReason::NotAttempted });
                continue;
            }
            if self.outstanding.len() + prepared.len() >= self.capacity {
                stop = true;
                rejected.push(RejectedSubmission { index, reason: RejectReason::CapacityExceeded });
                continue;
            }
            match self.driver.prepare(spec) {
                Ok(submission) => prepared.push((index, submission)),
                Err(PrepareError(detail)) => {
                    stop = true;
                    rejected.push(RejectedSubmission { index, reason: RejectReason::PrepareFailed(detail) });
                }
            }
        }

        // Pass 2 (commit): claims a RequestId before calling dispatch() -
        // even a dispatch() failure means the request reached the driver,
        // so it needs an ID to report against.
        let mut accepted = Vec::with_capacity(prepared.len());
        let mut dispatch_failed = false;
        for (index, submission) in prepared {
            if dispatch_failed {
                rejected.push(RejectedSubmission { index, reason: RejectReason::NotAttempted });
                continue;
            }

            let id = RequestId::new(self.ids.next_id());
            let mut state = StateMachine::new();
            state.accept().expect("fresh StateMachine is always NEW");

            match self.driver.dispatch(id, submission) {
                Ok(now_submitted) => {
                    if now_submitted {
                        state.mark_submitted().expect("just QUEUED - SUBMITTED transition must succeed");
                    }
                    self.outstanding.insert(id, InFlight { state });
                    accepted.push(RequestHandle::new(self.context_id, id));
                }
                Err(DispatchError(os_error)) => {
                    dispatch_failed = true;
                    rejected.push(RejectedSubmission { index, reason: RejectReason::DispatchFailed(os_error) });
                }
            }
        }

        SubmitReport { accepted, rejected }
    }

    pub fn submit_one(&mut self, spec: RequestSpec) -> Result<RequestHandle, RejectedSubmission> {
        let mut report = self.submit_many(vec![spec]);
        if let Some(handle) = report.accepted.pop() {
            Ok(handle)
        } else {
            Err(report.rejected.pop().expect("submit_many(1 item) always accepts or rejects exactly one"))
        }
    }

    /// Takes an opaque `RequestHandle`, not a bare `RequestId`: each
    /// `Engine`'s own `IdSequence` starts at 0 independently, so two
    /// different Engines' requests can share the same numeric ID. The
    /// `WrongContext` check below catches a handle from the wrong Engine
    /// before it can route to an unrelated same-numbered request.
    pub fn cancel(&mut self, handle: RequestHandle) -> Result<CancelStatus, EngineError> {
        if handle.context_id() != self.context_id {
            return Err(EngineError::WrongContext);
        }
        let id = handle.request_id();
        let inflight = self.outstanding.get_mut(&id).ok_or(EngineError::UnknownRequest)?;
        // AlreadyTerminal is usually answered by the caller from state it
        // already has, without reaching the engine - handling it here too
        // keeps this path safe to call defensively.
        let supports = self.driver.supports_inflight_cancel();
        let was_queued = inflight.state.state() == RequestState::Queued;
        let was_submitted = inflight.state.state() == RequestState::Submitted;
        let status = inflight.state.request_cancel(supports)?;

        match status {
            CancelStatus::CancelledBeforeStart if was_queued => {
                self.driver.cancel_queued(id);
                self.outstanding.remove(&id);
            }
            CancelStatus::Requested if was_submitted => {
                self.driver.cancel_inflight(id);
            }
            _ => {}
        }
        Ok(status)
    }

    /// Non-blocking: applies whatever the driver currently reports,
    /// removing any request that reaches a terminal state from the
    /// registry, and returns exactly those terminal completions. An event
    /// for an unknown/already-terminal ID is a driver bug, not a registry
    /// state to paper over - it panics rather than recovering silently.
    pub fn poll(&mut self) -> Vec<(RequestId, CompletionResult)> {
        let events = self.driver.poll();
        let mut completions = Vec::new();

        for event in events {
            match event {
                DriverEvent::Submitted(id) => {
                    let inflight = self
                        .outstanding
                        .get_mut(&id)
                        .unwrap_or_else(|| panic!("driver reported Submitted for unknown request {id:?}"));
                    inflight
                        .state
                        .mark_submitted()
                        .unwrap_or_else(|e| panic!("driver reported Submitted twice for {id:?}: {e}"));
                }
                DriverEvent::Completed(id, result) => {
                    let inflight = self
                        .outstanding
                        .remove(&id)
                        .unwrap_or_else(|| panic!("driver reported a completion for unknown/already-reaped request {id:?}"));
                    let mut state = inflight.state;
                    if matches!(result, CompletionResult::Cancelled) {
                        state
                            .confirm_cancelled()
                            .unwrap_or_else(|e| panic!("driver reported Cancelled from the wrong state for {id:?}: {e}"));
                    } else {
                        state
                            .complete(result.is_error())
                            .unwrap_or_else(|e| panic!("driver reported a duplicate completion for {id:?}: {e}"));
                    }
                    completions.push((id, result));
                }
            }
        }

        completions
    }

    /// Immediate, unconditional teardown for the `Drop` path: must not
    /// block and must not invoke Python. Takes `&mut self`, not `self` by
    /// value - a PyO3 bridge's `Engine` typically lives behind
    /// `Arc<Mutex<_>>` (so a worker thread's wake callback can reach it
    /// too), and `Drop` only ever gets `&mut self` there. Callers wanting
    /// a graceful `OPEN -> CLOSING -> CLOSED` sequence instead should use
    /// `begin_close()`/`finish_close()`.
    pub fn shutdown(&mut self) -> Vec<RequestId> {
        self.driver.shutdown()
    }

    /// `OPEN -> CLOSING`. Idempotent. Immediately cancels every `QUEUED`
    /// request (never reached the driver, so reaping it is certain and
    /// instant) and removes it from the registry, returning their IDs so
    /// the bridge can resolve their Python waiters right away instead of
    /// with the real result.
    ///
    /// Every `SUBMITTED` request also gets a best-effort cancel request
    /// (same semantics as `cancel()`), but stays in the registry - its
    /// real outcome only arrives later through `poll()`. Callers keep
    /// polling (their own deadline, GIL released) until
    /// `outstanding_count()` reaches zero, then call `finish_close()`.
    pub fn begin_close(&mut self) -> Vec<RequestId> {
        if self.context_state != ContextState::Open {
            return Vec::new();
        }
        self.context_state = ContextState::Closing;

        let queued_ids: Vec<RequestId> = self
            .outstanding
            .iter()
            .filter(|(_, inflight)| inflight.state.state() == RequestState::Queued)
            .map(|(id, _)| *id)
            .collect();
        for id in &queued_ids {
            self.driver.cancel_queued(*id);
            self.outstanding.remove(id);
        }

        let submitted_ids: Vec<RequestId> = self
            .outstanding
            .iter()
            .filter(|(_, inflight)| inflight.state.state() == RequestState::Submitted)
            .map(|(id, _)| *id)
            .collect();
        let supports = self.driver.supports_inflight_cancel();
        for id in submitted_ids {
            let inflight = self.outstanding.get_mut(&id).expect("just collected from outstanding");
            if let Ok(CancelStatus::Requested) = inflight.state.request_cancel(supports) {
                self.driver.cancel_inflight(id);
            }
        }

        queued_ids
    }

    /// Completes a graceful close started with `begin_close()`. Panics if
    /// requests are still outstanding - callers must drain them via
    /// `poll()` first (up to their own deadline; anything left after the
    /// deadline is a `Drop`-style `shutdown()` situation, not this one).
    /// Takes `&mut self` for the same reason `shutdown()` does.
    pub fn finish_close(&mut self) -> Vec<RequestId> {
        assert!(
            self.outstanding.is_empty(),
            "finish_close() called with {} request(s) still outstanding - poll() until empty first",
            self.outstanding.len(),
        );
        self.context_state = ContextState::Closed;
        self.driver.shutdown()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::spec::OsError;
    use std::cell::RefCell;
    use std::rc::Rc;

    /// A deterministic, fully caller-controlled `Driver` for testing the
    /// engine's own contract without any real kernel/thread pool.
    #[derive(Default)]
    struct FakeDriver {
        supports_inflight_cancel: bool,
        fail_next_prepare: bool,
        auto_submit_on_dispatch: bool,
        pending_events: Rc<RefCell<Vec<DriverEvent>>>,
        cancelled_inflight: Rc<RefCell<Vec<RequestId>>>,
        cancelled_queued: Rc<RefCell<Vec<RequestId>>>,
    }

    impl Driver for FakeDriver {
        type Submission = ();

        fn supports_inflight_cancel(&self) -> bool {
            self.supports_inflight_cancel
        }

        fn prepare(&mut self, _spec: RequestSpec) -> Result<Self::Submission, PrepareError> {
            if self.fail_next_prepare {
                self.fail_next_prepare = false;
                return Err(PrepareError("injected allocation failure".into()));
            }
            Ok(())
        }

        fn dispatch(&mut self, id: RequestId, _submission: Self::Submission) -> Result<bool, DispatchError> {
            if self.auto_submit_on_dispatch {
                Ok(true)
            } else {
                self.pending_events.borrow_mut().push(DriverEvent::Submitted(id));
                Ok(false)
            }
        }

        fn cancel_inflight(&mut self, id: RequestId) {
            self.cancelled_inflight.borrow_mut().push(id);
        }

        fn cancel_queued(&mut self, id: RequestId) {
            self.cancelled_queued.borrow_mut().push(id);
        }

        fn poll(&mut self) -> Vec<DriverEvent> {
            self.pending_events.borrow_mut().drain(..).collect()
        }

        fn shutdown(&mut self) -> Vec<RequestId> {
            Vec::new()
        }
    }

    fn read_spec() -> RequestSpec {
        RequestSpec::read(3, 0, 4096, 0).unwrap()
    }

    fn make_engine(capacity: usize, auto_submit: bool) -> Engine<FakeDriver> {
        // supports_inflight_cancel: true models linux_aio/io_uring; the
        // thread-backend "can't interrupt a running syscall" case is
        // covered by its own driver in cancel_unsupported_driver_leaves_request_running.
        let driver = FakeDriver {
            auto_submit_on_dispatch: auto_submit,
            supports_inflight_cancel: true,
            ..Default::default()
        };
        Engine::new(ContextId::new(1), capacity, driver)
    }

    #[test]
    fn submit_one_and_complete() {
        let mut engine = make_engine(8, true);
        let handle = engine.submit_one(read_spec()).unwrap();
        let id = handle.request_id();
        assert_eq!(engine.state_of(id), Some(RequestState::Submitted));
        engine
            .driver
            .pending_events
            .borrow_mut()
            .push(DriverEvent::Completed(id, CompletionResult::Read { buffer: vec![].into(), transferred: 0 }));
        let completions = engine.poll();
        assert_eq!(completions.len(), 1);
        assert_eq!(completions[0].0, id);
        assert_eq!(engine.outstanding_count(), 0);
    }

    #[test]
    fn deferred_submission_via_poll() {
        let mut engine = make_engine(8, false);
        let id = engine.submit_one(read_spec()).unwrap().request_id();
        assert_eq!(engine.state_of(id), Some(RequestState::Queued));
        let events = engine.poll();
        assert!(events.is_empty(), "Submitted events are not surfaced as completions");
        assert_eq!(engine.state_of(id), Some(RequestState::Submitted));
    }

    #[test]
    fn capacity_rejects_overflow_but_commits_accepted_prefix() {
        let mut engine = make_engine(2, true);
        let report = engine.submit_many(vec![read_spec(), read_spec(), read_spec()]);
        assert_eq!(report.accepted.len(), 2);
        assert_eq!(report.rejected.len(), 1);
        assert_eq!(report.rejected[0].reason, RejectReason::CapacityExceeded);
        assert_eq!(engine.outstanding_count(), 2);
    }

    #[test]
    fn allocation_failure_mid_batch_commits_accepted_prefix_and_stops() {
        let mut engine = make_engine(8, true);
        engine.driver.fail_next_prepare = false;
        let mut report = SubmitReport::default();
        {
            let r = engine.submit_many(vec![read_spec()]);
            report.accepted.extend(r.accepted);
        }
        engine.driver.fail_next_prepare = true;
        let r2 = engine.submit_many(vec![read_spec(), read_spec()]);
        assert_eq!(r2.accepted.len(), 0);
        assert_eq!(r2.rejected.len(), 2);
        assert!(matches!(r2.rejected[0].reason, RejectReason::PrepareFailed(_)));
        assert_eq!(r2.rejected[1].reason, RejectReason::NotAttempted);
        assert_eq!(engine.outstanding_count(), 1);
    }

    #[test]
    fn duplicate_completion_for_the_same_id_panics_instead_of_silently_double_freeing() {
        let mut engine = make_engine(8, true);
        let id = engine.submit_one(read_spec()).unwrap().request_id();
        engine.driver.pending_events.borrow_mut().push(DriverEvent::Completed(
            id,
            CompletionResult::Read { buffer: vec![].into(), transferred: 0 },
        ));
        engine.poll();
        // The ID has been fully reaped; a stale/duplicate second completion
        // for it must be loudly wrong, not a silent no-op or a crash later.
        engine.driver.pending_events.borrow_mut().push(DriverEvent::Completed(
            id,
            CompletionResult::Read { buffer: vec![].into(), transferred: 0 },
        ));
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| engine.poll()));
        assert!(result.is_err(), "duplicate completion for a reaped ID must panic, not succeed silently");
    }

    #[test]
    fn cancel_queued_removes_it_from_the_registry_immediately() {
        let mut engine = make_engine(8, false); // stays QUEUED until poll()
        let handle = engine.submit_one(read_spec()).unwrap();
        assert_eq!(engine.state_of(handle.request_id()), Some(RequestState::Queued));
        let status = engine.cancel(handle).unwrap();
        assert_eq!(status, CancelStatus::CancelledBeforeStart);
        assert_eq!(engine.outstanding_count(), 0, "cancelled-before-start requests are reaped immediately");
        assert_eq!(engine.driver.cancelled_queued.borrow().len(), 1);
    }

    #[test]
    fn cancel_inflight_race_can_still_complete() {
        let mut engine = make_engine(8, true);
        let handle = engine.submit_one(read_spec()).unwrap();
        let status = engine.cancel(handle).unwrap();
        assert_eq!(status, CancelStatus::Requested);
        assert_eq!(engine.outstanding_count(), 1, "still owned until the real completion arrives");
        assert_eq!(engine.driver.cancelled_inflight.borrow().len(), 1);

        engine.driver.pending_events.borrow_mut().push(DriverEvent::Completed(
            handle.request_id(),
            CompletionResult::Read { buffer: vec![].into(), transferred: 4096 },
        ));
        let completions = engine.poll();
        assert_eq!(completions.len(), 1);
        assert_eq!(engine.outstanding_count(), 0);
    }

    #[test]
    fn cancel_unsupported_driver_leaves_request_running() {
        let driver = FakeDriver { auto_submit_on_dispatch: true, supports_inflight_cancel: false, ..Default::default() };
        let mut engine = Engine::new(ContextId::new(1), 8, driver);
        let handle = engine.submit_one(read_spec()).unwrap();
        let status = engine.cancel(handle).unwrap();
        assert_eq!(status, CancelStatus::Unsupported);
        assert_eq!(engine.state_of(handle.request_id()), Some(RequestState::Submitted));
        assert!(engine.driver.cancelled_inflight.borrow().is_empty(), "must not even attempt it");
    }

    #[test]
    fn cancel_unknown_id_is_a_typed_error_not_a_panic() {
        let mut engine = make_engine(8, true);
        let bogus = RequestHandle::new(engine.context_id(), RequestId::new(9999));
        assert_eq!(engine.cancel(bogus), Err(EngineError::UnknownRequest));
    }

    #[test]
    fn cancel_from_a_different_context_is_rejected_before_touching_the_registry() {
        // Each Engine's own IdSequence starts at 0 independently, so both
        // assign local RequestId(0) - a handle from engine_a must never be
        // usable to cancel engine_b's same-numbered, unrelated request.
        let mut engine_a = Engine::new(ContextId::new(1), 8, FakeDriver { auto_submit_on_dispatch: true, ..Default::default() });
        let mut engine_b = Engine::new(ContextId::new(2), 8, FakeDriver { auto_submit_on_dispatch: true, ..Default::default() });

        let handle_a = engine_a.submit_one(read_spec()).unwrap();
        let handle_b = engine_b.submit_one(read_spec()).unwrap();
        assert_eq!(handle_a.request_id(), handle_b.request_id(), "both engines assign the same local RequestId(0)");

        assert_eq!(engine_b.cancel(handle_a), Err(EngineError::WrongContext));
        assert_eq!(engine_b.state_of(handle_b.request_id()), Some(RequestState::Submitted));
        assert_eq!(engine_b.outstanding_count(), 1);
        assert!(engine_b.driver.cancelled_inflight.borrow().is_empty(), "must not reach the driver at all");
        assert_eq!(engine_a.state_of(handle_a.request_id()), Some(RequestState::Submitted));
    }

    #[test]
    fn os_error_completion_marks_failed_not_completed() {
        let mut engine = make_engine(8, true);
        let id = engine.submit_one(read_spec()).unwrap().request_id();
        engine
            .driver
            .pending_events
            .borrow_mut()
            .push(DriverEvent::Completed(id, CompletionResult::Error(OsError::from_raw(5))));
        let completions = engine.poll();
        assert!(completions[0].1.is_error());
    }

    #[test]
    fn closed_context_rejects_new_submissions() {
        let mut engine = make_engine(8, true);
        assert_eq!(engine.context_state(), ContextState::Open);
        let queued_before_close = engine.begin_close();
        assert!(queued_before_close.is_empty());
        assert_eq!(engine.context_state(), ContextState::Closing);

        let report = engine.submit_many(vec![read_spec()]);
        assert!(report.accepted.is_empty());
        assert_eq!(report.rejected[0].reason, RejectReason::ContextClosed);
    }

    #[test]
    fn begin_close_reaps_queued_immediately_and_cancels_inflight() {
        let mut engine = make_engine(8, false); // stays QUEUED until poll()
        let queued_id = engine.submit_one(read_spec()).unwrap().request_id();
        assert_eq!(engine.state_of(queued_id), Some(RequestState::Queued));

        let mut engine2 = make_engine(8, true); // dispatches immediately
        let submitted_id = engine2.submit_one(read_spec()).unwrap().request_id();
        assert_eq!(engine2.state_of(submitted_id), Some(RequestState::Submitted));

        let reaped = engine.begin_close();
        assert_eq!(reaped, vec![queued_id]);
        assert_eq!(engine.outstanding_count(), 0);
        assert_eq!(engine.driver.cancelled_queued.borrow().len(), 1);

        engine2.begin_close();
        assert_eq!(engine2.outstanding_count(), 1, "in-flight request stays until its real completion arrives");
        assert_eq!(engine2.state_of(submitted_id), Some(RequestState::CancelRequested));
        assert_eq!(engine2.driver.cancelled_inflight.borrow().len(), 1);
    }

    #[test]
    fn finish_close_panics_if_requests_still_outstanding() {
        let mut engine = make_engine(8, true);
        engine.submit_one(read_spec()).unwrap();
        engine.begin_close();
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| engine.finish_close()));
        assert!(result.is_err(), "finish_close() must refuse to complete with a non-empty registry");
    }

    #[test]
    fn finish_close_succeeds_once_registry_is_empty() {
        let mut engine = make_engine(8, true);
        engine.begin_close();
        engine.finish_close();
    }
}
