//! Numeric identifiers used in place of raw Python object pointers.
//!
//! Kernel/worker completion paths tag their work with these instead of a
//! `PyObject*` (`aio_data`/`user_data`/queue item). Neither ID is ever
//! reused within its Context's lifetime (a wrapping `u64` counter would
//! take billions of years of sustained submission to wrap on any real
//! workload).

#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, PartialOrd, Ord)]
pub struct ContextId(u64);

#[repr(transparent)]
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, PartialOrd, Ord)]
pub struct RequestId(u64);

/// Monotonic, never-reused ID source for one Context's lifetime.
#[derive(Debug, Default)]
pub struct IdSequence(u64);

impl IdSequence {
    pub fn next_id(&mut self) -> u64 {
        let id = self.0;
        self.0 = self
            .0
            .checked_add(1)
            .expect("request ID sequence exhausted - not reachable on any real workload");
        id
    }
}

impl ContextId {
    pub fn new(raw: u64) -> Self {
        ContextId(raw)
    }
}

impl RequestId {
    pub fn new(raw: u64) -> Self {
        RequestId(raw)
    }

    /// Round-trips through a kernel-visible numeric tag - `linux_aio`'s
    /// `aio_data`, `io_uring`'s `user_data` - in place of a raw Python
    /// object pointer. `as_u64()` encodes it going out; `new()` (same
    /// constructor used for freshly-issued IDs) decodes it coming back
    /// from a completion event.
    pub fn as_u64(&self) -> u64 {
        self.0
    }
}

/// Opaque, caller-facing identity for an accepted request - a `RequestId`
/// alone is only unique *within* the `Engine` that issued it (each
/// `Engine`'s own `IdSequence` starts at 0 independently), so a bare
/// `RequestId` handed to the wrong `Context`'s `cancel()` could silently
/// collide with a same-numbered, unrelated request there.
/// `Engine::submit_many`/`submit_one` return this (not a bare
/// `RequestId`), and `Engine::cancel` rejects a `context_id` mismatch
/// before ever consulting its own registry.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct RequestHandle {
    context_id: ContextId,
    request_id: RequestId,
}

impl RequestHandle {
    pub fn new(context_id: ContextId, request_id: RequestId) -> Self {
        RequestHandle { context_id, request_id }
    }

    pub fn context_id(&self) -> ContextId {
        self.context_id
    }

    pub fn request_id(&self) -> RequestId {
        self.request_id
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn id_sequence_never_repeats() {
        let mut seq = IdSequence::default();
        let a = seq.next_id();
        let b = seq.next_id();
        let c = seq.next_id();
        assert_eq!([a, b, c], [0, 1, 2]);
    }
}
