"""
Behavior specific to thread_aio's own native worker-thread-pool design -
not a bug pattern shared with the other backends, so these don't belong in
the cross-backend parametrized suite. Skipped outright wherever thread_aio
itself isn't available.
"""
import gc
import sys
import threading
import time
import weakref

import pytest

from caio import thread_aio

if thread_aio is None:
    pytest.skip("thread_aio backend not available on this platform", allow_module_level=True)


def test_queue_overflow_allows_retry_with_original_data(tmp_path):
    """An operation rejected because the worker pool's queue is full must
    remain fully retryable afterward - not permanently stuck (as "already
    in progress" forever) and not silently missing its original payload on
    the later, successful resubmit.

    Submits 5 ops at once with capacity=1 (not just 2): the single worker
    thread *can* race to dequeue the very first one almost immediately
    (dequeuing is a plain C-side queue pop, not gated by the GIL at all) -
    but it can't dequeue a second one until the first fully *completes*,
    which needs the GIL this whole submit() call holds throughout. So at
    most one op can ever be raced away like that, making the LAST op in a
    5-op batch guaranteed to overflow regardless of exactly how that race
    resolves for the first one.

    The raced-away first op means a second op can sit in the one-slot
    queue when the first op's callback fires, so the resubmit below can
    still hit a full queue for a moment - reliably so on a single CPU
    (GitHub #79). Retry until it is accepted: the contract under test is
    "not permanently stuck", not "accepted on the first try".
    """
    with open(str(tmp_path / "temp.bin"), "wb+") as f:
        fd = f.fileno()
        ctx = thread_aio.Context(max_requests=1, pool_size=1)

        payload = b"hello"
        ops = [thread_aio.Operation.write(payload, fd, i * len(payload)) for i in range(5)]

        # ops[0] is deterministically the very first one checked, so it's
        # always accepted (capacity=1, nothing else has run yet) -
        # retrying the rejected one only has anywhere to go once this one
        # actually finishes and the worker is free again.
        first_done = threading.Event()
        ops[0].set_callback(lambda _r: first_done.set())

        with pytest.raises(RuntimeError):
            ctx.submit(*ops)

        assert first_done.wait(timeout=30.0), "the first op should have been accepted and completed"

        rejected = ops[-1]

        done = threading.Event()
        rejected.set_callback(lambda _r: done.set())
        deadline = time.monotonic() + 30.0
        while True:
            try:
                resubmitted = ctx.submit(rejected)
            except RuntimeError:
                assert time.monotonic() < deadline, "queue never drained for the retry"
                time.sleep(0.001)
            else:
                break
        assert resubmitted == 1, "a queue-rejected operation must not be permanently stuck"
        assert done.wait(timeout=30.0), "retried operation must actually run"
        assert rejected.result == len(payload), f"expected a full write, got result={rejected.result}"

    with open(str(tmp_path / "temp.bin"), "rb") as rf:
        rf.seek(4 * len(payload))
        assert rf.read(len(payload)) == payload, (
            "resubmitted operation must write its ORIGINAL payload, not lost/empty data"
        )


def _vm_data_mb():
    """Virtual data size of this process in MiB, from /proc (Linux only)."""
    with open("/proc/self/status") as status:
        for line in status:
            if line.startswith("VmData:"):
                return int(line.split()[1]) // 1024
    raise RuntimeError("VmData not found")


@pytest.mark.skipif(sys.platform != "linux", reason="uses /proc/self/status")
def test_context_released_by_its_own_worker_frees_the_pool(tmp_path):
    """A completion callback can drop the last reference to the Context.
    The worker that runs the callback then runs the Context destructor
    and tears the pool down from inside one of its own threads.

    pthread_join() of the calling thread fails with EDEADLK. That used to
    skip freeing the pool, so every such Context leaked its queue and the
    unjoined worker's 8 MiB stack. VmData counts that memory even though
    it is never touched, so the growth over 40 iterations is ~360 MiB
    with the leak and a few MiB without it.

    The callback blocks until the main thread has dropped its reference,
    so the worker's own DECREF is the last one. On a free-threaded build
    biased reference counting can still move the dealloc to the owning
    thread, so this test proves the leak only on GIL builds; it still
    exercises the teardown path on both.
    """
    iterations = 40
    with open(str(tmp_path / "temp.bin"), "wb+") as f:
        fd = f.fileno()
        keep = []
        dealloc_threads = set()

        def one():
            done = threading.Event()
            main_dropped = threading.Event()
            ctx = thread_aio.Context(max_requests=60000, pool_size=2)
            weakref.finalize(
                ctx,
                lambda: dealloc_threads.add(threading.current_thread().name),
            )
            op = thread_aio.Operation.write(b"x", fd, 0)

            def callback(_result):
                main_dropped.wait(5)
                done.set()

            op.set_callback(callback)
            assert ctx.submit(op) == 1
            del ctx
            main_dropped.set()
            assert done.wait(5), "write never completed"
            keep.append(op)

        one()  # warm up allocator arenas before measuring
        gc.collect()
        before = _vm_data_mb()
        for _ in range(iterations):
            one()
        gc.collect()
        time.sleep(0.2)
        growth = _vm_data_mb() - before

    assert dealloc_threads, "no Context was deallocated"
    assert growth < 100, (
        f"VmData grew by {growth} MiB over {iterations} Contexts released "
        f"on {sorted(dealloc_threads)} - the pool or a worker stack leaks"
    )
