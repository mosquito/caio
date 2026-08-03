"""
Behavior specific to thread_aio's own native worker-thread-pool design -
not a bug pattern shared with the other backends, so these don't belong in
the cross-backend parametrized suite. Skipped outright wherever thread_aio
itself isn't available.
"""
import threading

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
        resubmitted = ctx.submit(rejected)
        assert resubmitted == 1, "a queue-rejected operation must not be permanently stuck"
        assert done.wait(timeout=30.0), "retried operation must actually run"
        assert rejected.result == len(payload), f"expected a full write, got result={rejected.result}"

    with open(str(tmp_path / "temp.bin"), "rb") as rf:
        rf.seek(4 * len(payload))
        assert rf.read(len(payload)) == payload, (
            "resubmitted operation must write its ORIGINAL payload, not lost/empty data"
        )
