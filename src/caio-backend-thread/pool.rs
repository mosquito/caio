//! A small bounded thread pool, replacing the vendored pthread-based
//! `threadpool.c`/`.h`. Semantics deliberately mirror the original:
//! - `pool_size` worker threads, started up front and kept alive.
//! - a bounded FIFO queue of capacity `queue_size`; submitting past that
//!   capacity is rejected rather than blocking.
//! - shutdown cancels whatever is left queued-but-not-yet-started (dropping
//!   each `Task` properly releases its Python reference, without running
//!   it) and waits, up to a bounded deadline, for every worker to exit -
//!   see `join_workers_with_deadline`'s own doc comment for what happens
//!   to a worker still running past that deadline. A worker already
//!   *running* a task when shutdown is signaled always finishes that one
//!   task first (there's no way to interrupt a blocking syscall
//!   mid-flight); only tasks that never started get cancelled.

use std::collections::VecDeque;
use std::sync::{Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

/// Re-exported so a bridge's own `Drop` can size its shutdown-reap
/// deadline the same way this pool's own docs describe it - see
/// `join_workers_with_deadline`'s doc comment for what it bounds and why.
pub const DROP_REAP_TIMEOUT_SECS: u64 = 30;

pub struct Task {
    pub run: Box<dyn FnOnce() + Send>,
}

struct QueueState {
    tasks: VecDeque<Task>,
    capacity: usize,
    shutdown: bool,
}

struct Shared {
    state: Mutex<QueueState>,
    condvar: Condvar,
}

#[derive(Debug)]
pub struct SubmitError;

pub struct Pool {
    shared: Arc<Shared>,
    workers: Vec<JoinHandle<()>>,
}

impl Pool {
    pub fn new(pool_size: usize, queue_size: usize) -> Self {
        let shared = Arc::new(Shared {
            state: Mutex::new(QueueState {
                tasks: VecDeque::new(),
                capacity: queue_size,
                shutdown: false,
            }),
            condvar: Condvar::new(),
        });

        let workers = (0..pool_size)
            .map(|_| {
                let shared = Arc::clone(&shared);
                thread::spawn(move || worker_loop(shared))
            })
            .collect();

        Pool { shared, workers }
    }

    /// Enqueues a task. Returns `Err(SubmitError)` if the queue is full.
    pub fn submit(&self, task: Task) -> Result<(), SubmitError> {
        let mut state = self.shared.state.lock().unwrap();

        if state.tasks.len() >= state.capacity {
            return Err(SubmitError);
        }

        state.tasks.push_back(task);
        drop(state);
        self.shared.condvar.notify_one();
        Ok(())
    }

    /// Checks whether the queue has room for one more task, without
    /// enqueuing anything - lets a caller skip submission-adjacent work
    /// for something about to be rejected anyway. Only a worker thread can
    /// shrink the queue, so a stale answer here is only ever pessimistic.
    pub fn has_capacity(&self) -> bool {
        let state = self.shared.state.lock().unwrap();
        state.tasks.len() < state.capacity
    }

    /// Sets the shutdown flag and wakes every worker - fast, never blocks.
    /// Split out from joining so a caller reaching this through another
    /// lock (e.g. a PyO3 bridge's `Engine` mutex, shared with a worker's
    /// own completion callback) can release that lock before the actual
    /// blocking join - holding it across the join risks that same worker
    /// needing the lock to finish its last callback before it can exit.
    pub fn signal_shutdown(&self) {
        {
            let mut state = self.shared.state.lock().unwrap();
            state.shutdown = true;
        }
        self.shared.condvar.notify_all();
    }

    /// Takes ownership of the worker `JoinHandle`s, leaving this `Pool`'s
    /// own list empty. Callers should call `signal_shutdown()` first, then
    /// drop whatever outer lock they're holding, *then* join the returned
    /// handles - joining a `JoinHandle` itself needs no lock on this
    /// `Pool` at all once it's been taken out.
    pub fn take_workers(&mut self) -> Vec<JoinHandle<()>> {
        std::mem::take(&mut self.workers)
    }

    /// Signals shutdown and waits up to `DROP_REAP_TIMEOUT_SECS` for every
    /// worker to exit - see `join_workers_with_deadline`'s own doc comment
    /// for what happens to a worker still running past that deadline.
    /// Callers holding a lock a worker might need to finish its own last
    /// callback (e.g. a PyO3 bridge's `Engine` mutex) should use
    /// `signal_shutdown()`/`take_workers()` directly instead, dropping
    /// that lock before joining.
    pub fn shutdown_and_join(&mut self) {
        self.signal_shutdown();
        join_workers_with_deadline(self.take_workers(), Duration::from_secs(DROP_REAP_TIMEOUT_SECS));
    }
}

/// Waits up to `timeout` for every worker to exit (self-joins are detached
/// instead - a completion callback dropping the last Context reference
/// from its own worker thread would otherwise deadlock/panic on
/// pthread_join's EDEADLK). `JoinHandle::join()` has no built-in timeout,
/// so this spawns one "reaper" thread per worker to block on its own
/// `join()` indefinitely, and waits on a shared countdown with a deadline
/// instead of on the joins directly. A worker still running past the
/// deadline is simply not waited for any further - dropping its
/// `JoinHandle` neither kills nor leaks the thread, it just stops
/// observing when it exits. Returns the number of workers still
/// outstanding when the deadline passed (0 means all exited in time).
pub fn join_workers_with_deadline(workers: Vec<JoinHandle<()>>, timeout: Duration) -> usize {
    let current = std::thread::current().id();
    let mut foreign = Vec::with_capacity(workers.len());
    for handle in workers {
        if handle.thread().id() == current {
            drop(handle);
            continue;
        }
        foreign.push(handle);
    }
    if foreign.is_empty() {
        return 0;
    }

    let remaining = Arc::new((Mutex::new(foreign.len()), Condvar::new()));
    for handle in foreign {
        let remaining = Arc::clone(&remaining);
        thread::spawn(move || {
            let _ = handle.join();
            let (count, condvar) = &*remaining;
            *count.lock().unwrap() -= 1;
            condvar.notify_all();
        });
    }

    let (count, condvar) = &*remaining;
    let guard = count.lock().unwrap();
    let (guard, _) = condvar.wait_timeout_while(guard, timeout, |n| *n > 0).unwrap();
    *guard
}

fn worker_loop(shared: Arc<Shared>) {
    loop {
        let mut state = shared.state.lock().unwrap();

        let task = loop {
            // Checked before popping, not after: once shutdown is
            // signaled, a still-queued-but-not-yet-started task is
            // cancelled by simply dropping it here (releasing whatever
            // Python reference its closure holds - safe even without the
            // GIL attached, since PyO3's `Py<T>` defers the actual decref
            // to whenever the GIL is next acquired by *any* thread) rather
            // than run to completion. Drop/shutdown must not wait out an
            // arbitrarily large backlog of queued-but-not-yet-started
            // work - only whatever a worker is already actively running,
            // which can't be interrupted anyway.
            if state.shutdown {
                break None;
            }
            if let Some(task) = state.tasks.pop_front() {
                break Some(task);
            }
            state = shared.condvar.wait(state).unwrap();
        };

        drop(state);

        match task {
            Some(task) => (task.run)(),
            None => return,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::mpsc;

    #[test]
    fn queued_task_is_cancelled_not_run_once_shutdown_is_signaled() {
        let pool = Pool::new(1, 8);
        let (unblock_tx, unblock_rx) = mpsc::channel::<()>();
        let first_task_started = Arc::new(AtomicBool::new(false));
        let first_task_started_writer = Arc::clone(&first_task_started);
        pool.submit(Task {
            run: Box::new(move || {
                first_task_started_writer.store(true, Ordering::SeqCst);
                let _ = unblock_rx.recv();
            }),
        })
        .unwrap();

        // The pool's single worker must actually be busy running the
        // first task (not still waiting on the queue) before the second
        // task is submitted - otherwise it could race in and run task 2
        // right away instead of leaving it genuinely queued.
        while !first_task_started.load(Ordering::SeqCst) {
            std::thread::yield_now();
        }

        let second_task_ran = Arc::new(AtomicBool::new(false));
        let second_task_ran_writer = Arc::clone(&second_task_ran);
        pool.submit(Task { run: Box::new(move || second_task_ran_writer.store(true, Ordering::SeqCst)) }).unwrap();

        // Shutdown signaled while task 2 is still sitting in the queue,
        // task 1 still blocked mid-flight.
        pool.signal_shutdown();
        unblock_tx.send(()).unwrap(); // let task 1 (and the worker) finish

        let mut pool = pool;
        let remaining = join_workers_with_deadline(pool.take_workers(), Duration::from_secs(5));
        assert_eq!(remaining, 0, "the single worker should have exited well within the deadline");
        assert!(
            !second_task_ran.load(Ordering::SeqCst),
            "a task still queued (never started) when shutdown was signaled must be cancelled, not run"
        );
    }

    #[test]
    fn join_workers_with_deadline_gives_up_on_a_stuck_worker_without_hanging() {
        let pool = Pool::new(1, 8);
        let (unblock_tx, unblock_rx) = mpsc::channel::<()>();
        let started = Arc::new(AtomicBool::new(false));
        let started_writer = Arc::clone(&started);
        pool.submit(Task {
            run: Box::new(move || {
                started_writer.store(true, Ordering::SeqCst);
                let _ = unblock_rx.recv();
            }),
        })
        .unwrap();

        // Must actually be running (blocked inside the task) before
        // shutdown is signaled - otherwise the worker could still be
        // waiting on the queue and simply exit per the "cancel not yet
        // started" behavior above, never actually getting stuck at all.
        while !started.load(Ordering::SeqCst) {
            std::thread::yield_now();
        }

        pool.signal_shutdown();
        let mut pool = pool;
        let started = std::time::Instant::now();
        let remaining = join_workers_with_deadline(pool.take_workers(), Duration::from_millis(50));
        assert_eq!(remaining, 1, "the worker is still blocked - it must be reported as outstanding, not silently dropped");
        assert!(
            started.elapsed() < Duration::from_secs(2),
            "must give up at the deadline, not block until the worker actually finishes"
        );

        // Let the leaked worker/reaper threads actually finish instead of
        // leaving a permanently-blocked thread running for the rest of
        // the test binary's lifetime.
        unblock_tx.send(()).unwrap();
    }
}
