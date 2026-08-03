//! The `Operation` state machine. One type, one lifecycle, no separate
//! `Request` type: `NEW` is the only state `accept()` (submit) succeeds
//! from, so a used/unused flag is redundant - the state itself already
//! says whether this `Operation` has ever been submitted.
//!
//! ```text
//! NEW -> QUEUED -> SUBMITTED -> COMPLETED
//!         │          │       └──> FAILED
//!         │          └──────────> CANCEL_REQUESTED -> CANCELLED
//!         │                                         ├──> COMPLETED
//!         │                                         └──> FAILED
//!         └─────────────────────> CANCELLED
//! ```

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RequestState {
    New,
    Queued,
    Submitted,
    CancelRequested,
    Completed,
    Failed,
    Cancelled,
}

impl RequestState {
    pub fn is_terminal(self) -> bool {
        matches!(self, RequestState::Completed | RequestState::Failed | RequestState::Cancelled)
    }
}

/// What a `cancel()` call reports back, distinct from `RequestState`:
/// `RequestState` is the operation's own persistent, Python-visible state;
/// `CancelStatus` is the one-shot answer to "what did calling cancel() just
/// do", which may not change the state at all (`AlreadyTerminal`,
/// `Unsupported`) or may (`CancelledBeforeStart`, `Requested`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CancelStatus {
    /// Cancellation was requested from the driver; the target may still
    /// complete successfully (a race this crate does not try to hide).
    Requested,
    /// The operation was still `QUEUED` (never reached the driver at all),
    /// so cancellation is immediate and certain - no `CANCEL_REQUESTED`
    /// limbo state needed.
    CancelledBeforeStart,
    /// Already `COMPLETED`/`FAILED`/`CANCELLED`. Not an error: cancel() is
    /// idempotent-safe to call on anything.
    AlreadyTerminal,
    /// The driver cannot attempt to interrupt a request already dispatched
    /// to the kernel/pool (e.g. thread backend mid-syscall). The request's
    /// state is unchanged - it will complete normally.
    Unsupported,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InvalidTransition {
    pub from: RequestState,
    pub attempted: &'static str,
}

impl std::fmt::Display for InvalidTransition {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "invalid transition from {:?}: {}", self.from, self.attempted)
    }
}

impl std::error::Error for InvalidTransition {}

#[derive(Debug, Clone, Copy)]
pub struct StateMachine {
    state: RequestState,
}

impl Default for StateMachine {
    fn default() -> Self {
        StateMachine { state: RequestState::New }
    }
}

impl StateMachine {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn state(&self) -> RequestState {
        self.state
    }

    /// `NEW -> QUEUED`. This *is* the "operation already used" guard - see
    /// the module doc comment - so it is the only way any caller (Python
    /// bridge, engine, tests) should ever reject a resubmit.
    pub fn accept(&mut self) -> Result<(), InvalidTransition> {
        if self.state != RequestState::New {
            return Err(InvalidTransition { from: self.state, attempted: "accept (NEW -> QUEUED)" });
        }
        self.state = RequestState::Queued;
        Ok(())
    }

    /// `QUEUED -> SUBMITTED`, once the driver actually dispatches this
    /// request to the kernel/pool.
    pub fn mark_submitted(&mut self) -> Result<(), InvalidTransition> {
        if self.state != RequestState::Queued {
            return Err(InvalidTransition {
                from: self.state,
                attempted: "mark_submitted (QUEUED -> SUBMITTED)",
            });
        }
        self.state = RequestState::Submitted;
        Ok(())
    }

    /// Applies a terminal outcome reported by the driver. Valid from
    /// `SUBMITTED` (the normal path) or `CANCEL_REQUESTED` (a cancel raced
    /// with the real completion and lost).
    pub fn complete(&mut self, failed: bool) -> Result<(), InvalidTransition> {
        match self.state {
            RequestState::Submitted | RequestState::CancelRequested => {
                self.state = if failed { RequestState::Failed } else { RequestState::Completed };
                Ok(())
            }
            _ => Err(InvalidTransition { from: self.state, attempted: "complete" }),
        }
    }

    /// The driver's own cancellation completed (as opposed to the target
    /// racing to a normal completion first). Only reachable from
    /// `CANCEL_REQUESTED`.
    pub fn confirm_cancelled(&mut self) -> Result<(), InvalidTransition> {
        if self.state != RequestState::CancelRequested {
            return Err(InvalidTransition {
                from: self.state,
                attempted: "confirm_cancelled (CANCEL_REQUESTED -> CANCELLED)",
            });
        }
        self.state = RequestState::Cancelled;
        Ok(())
    }

    /// Requests cancellation. `driver_supports_inflight_cancel` reflects a
    /// driver *capability*, not this operation's state - e.g. the thread
    /// backend can drop an unstarted queued job but can't interrupt a
    /// blocking syscall already running, whereas `linux_aio`/`io_uring` can
    /// always attempt an async cancel on a dispatched request. Rejects
    /// (`Err`) only for `NEW`: nothing has been submitted yet, so there is
    /// no registry entry to target - this is a caller error, not a
    /// `CancelStatus` outcome. Every other state has a well-defined,
    /// infallible `CancelStatus` answer, including calling this twice in a
    /// row on the same still-pending request (idempotent: `Requested`
    /// again, no double transition).
    pub fn request_cancel(&mut self, driver_supports_inflight_cancel: bool) -> Result<CancelStatus, InvalidTransition> {
        match self.state {
            RequestState::New => {
                Err(InvalidTransition { from: self.state, attempted: "cancel (nothing submitted yet)" })
            }
            RequestState::Queued => {
                self.state = RequestState::Cancelled;
                Ok(CancelStatus::CancelledBeforeStart)
            }
            RequestState::Submitted => {
                if driver_supports_inflight_cancel {
                    self.state = RequestState::CancelRequested;
                    Ok(CancelStatus::Requested)
                } else {
                    Ok(CancelStatus::Unsupported)
                }
            }
            RequestState::CancelRequested => Ok(CancelStatus::Requested),
            RequestState::Completed | RequestState::Failed | RequestState::Cancelled => {
                Ok(CancelStatus::AlreadyTerminal)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn happy_path_read_write() {
        let mut sm = StateMachine::new();
        assert_eq!(sm.state(), RequestState::New);
        sm.accept().unwrap();
        assert_eq!(sm.state(), RequestState::Queued);
        sm.mark_submitted().unwrap();
        assert_eq!(sm.state(), RequestState::Submitted);
        sm.complete(false).unwrap();
        assert_eq!(sm.state(), RequestState::Completed);
        assert!(sm.state().is_terminal());
    }

    #[test]
    fn failed_completion() {
        let mut sm = StateMachine::new();
        sm.accept().unwrap();
        sm.mark_submitted().unwrap();
        sm.complete(true).unwrap();
        assert_eq!(sm.state(), RequestState::Failed);
    }

    #[test]
    fn cannot_resubmit_without_a_used_flag() {
        let mut sm = StateMachine::new();
        sm.accept().unwrap();
        assert!(sm.accept().is_err(), "second accept() must fail - no resubmit");
        sm.mark_submitted().unwrap();
        assert!(sm.accept().is_err());
        sm.complete(false).unwrap();
        assert!(sm.accept().is_err(), "terminal Operation must still refuse resubmission");
    }

    #[test]
    fn cancel_before_submission_is_immediate() {
        let mut sm = StateMachine::new();
        sm.accept().unwrap();
        let status = sm.request_cancel(true).unwrap();
        assert_eq!(status, CancelStatus::CancelledBeforeStart);
        assert_eq!(sm.state(), RequestState::Cancelled);
        assert!(sm.state().is_terminal());
    }

    #[test]
    fn cancel_in_flight_is_requested_not_terminal() {
        let mut sm = StateMachine::new();
        sm.accept().unwrap();
        sm.mark_submitted().unwrap();
        let status = sm.request_cancel(true).unwrap();
        assert_eq!(status, CancelStatus::Requested);
        assert_eq!(sm.state(), RequestState::CancelRequested);
        assert!(!sm.state().is_terminal(), "CANCEL_REQUESTED is not terminal");
    }

    #[test]
    fn cancel_race_can_still_complete_successfully() {
        let mut sm = StateMachine::new();
        sm.accept().unwrap();
        sm.mark_submitted().unwrap();
        sm.request_cancel(true).unwrap();
        // The kernel finished the real work before the cancel landed.
        sm.complete(false).unwrap();
        assert_eq!(sm.state(), RequestState::Completed);
    }

    #[test]
    fn cancel_race_can_resolve_to_cancelled() {
        let mut sm = StateMachine::new();
        sm.accept().unwrap();
        sm.mark_submitted().unwrap();
        sm.request_cancel(true).unwrap();
        sm.confirm_cancelled().unwrap();
        assert_eq!(sm.state(), RequestState::Cancelled);
    }

    #[test]
    fn repeated_cancel_is_idempotent_not_a_double_transition() {
        let mut sm = StateMachine::new();
        sm.accept().unwrap();
        sm.mark_submitted().unwrap();
        assert_eq!(sm.request_cancel(true).unwrap(), CancelStatus::Requested);
        assert_eq!(sm.request_cancel(true).unwrap(), CancelStatus::Requested);
        assert_eq!(sm.state(), RequestState::CancelRequested);
    }

    #[test]
    fn cancel_on_driver_without_inflight_support_leaves_state_unchanged() {
        // Models thread_aio: a running blocking syscall can't be interrupted.
        let mut sm = StateMachine::new();
        sm.accept().unwrap();
        sm.mark_submitted().unwrap();
        let status = sm.request_cancel(false).unwrap();
        assert_eq!(status, CancelStatus::Unsupported);
        assert_eq!(sm.state(), RequestState::Submitted, "state must not move to CANCEL_REQUESTED");
        // The real completion still arrives normally afterward.
        sm.complete(false).unwrap();
        assert_eq!(sm.state(), RequestState::Completed);
    }

    #[test]
    fn cancel_on_new_is_a_caller_error_not_a_status() {
        let mut sm = StateMachine::new();
        assert!(sm.request_cancel(true).is_err(), "nothing submitted yet - not even QUEUED");
    }

    #[test]
    fn cancel_on_terminal_is_a_safe_no_op() {
        for accept_then in [true, false] {
            let mut sm = StateMachine::new();
            sm.accept().unwrap();
            sm.mark_submitted().unwrap();
            if accept_then {
                sm.complete(false).unwrap();
            } else {
                sm.complete(true).unwrap();
            }
            let before = sm.state();
            let status = sm.request_cancel(true).unwrap();
            assert_eq!(status, CancelStatus::AlreadyTerminal);
            assert_eq!(sm.state(), before, "AlreadyTerminal must not change state");
        }
    }

    #[test]
    fn every_terminal_state_rejects_further_transitions() {
        let terminal_builders: [fn() -> StateMachine; 3] = [
            || {
                let mut sm = StateMachine::new();
                sm.accept().unwrap();
                sm.mark_submitted().unwrap();
                sm.complete(false).unwrap();
                sm
            },
            || {
                let mut sm = StateMachine::new();
                sm.accept().unwrap();
                sm.mark_submitted().unwrap();
                sm.complete(true).unwrap();
                sm
            },
            || {
                let mut sm = StateMachine::new();
                sm.accept().unwrap();
                sm.request_cancel(true).unwrap();
                sm
            },
        ];

        for build in terminal_builders {
            let mut sm = build();
            assert!(sm.state().is_terminal());
            assert!(sm.accept().is_err());
            assert!(sm.mark_submitted().is_err());
            assert!(sm.complete(false).is_err());
            assert!(sm.confirm_cancelled().is_err());
        }
    }
}
