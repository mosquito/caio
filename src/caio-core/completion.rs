//! What a finished request produced - an enum, not a `result=-1`/`error`/
//! `nbytes` triple.

use crate::spec::OsError;

#[derive(Debug)]
pub enum CompletionResult {
    Read { buffer: Box<[u8]>, transferred: usize },
    Write { transferred: usize },
    Sync,
    Cancelled,
    Error(OsError),
}

impl CompletionResult {
    pub fn is_error(&self) -> bool {
        matches!(self, CompletionResult::Error(_))
    }
}
