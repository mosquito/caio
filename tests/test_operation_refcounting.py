import gc
import sys
import tempfile
import time

import pytest
from conftest import wait_until


def test_get_value_on_fsync_does_not_corrupt_none_refcount(backend):
    """get_value() on fsync/fdsync must return None on every backend. The 3
    C backends used to `return Py_None` instead of Py_RETURN_NONE there - a
    borrowed reference handed out as if it were new; run it enough times
    that an under-refcounted None would show up as a wrong count (or, in
    the worst case, a crash from None being freed). max_requests=256 is
    just generous headroom for 200 in-flight ops submitted without pacing -
    not testing capacity limits here."""
    with tempfile.NamedTemporaryFile() as f:
        ctx = backend.Context(max_requests=256)
        results = []

        for _ in range(200):
            op = backend.Operation.fsync(f.fileno())
            op.set_callback(results.append)
            ctx.submit(op)

        assert wait_until(ctx, lambda: len(results) >= 200)

        op = backend.Operation.fsync(f.fileno())
        done = []
        op.set_callback(done.append)
        ctx.submit(op)
        assert wait_until(ctx, lambda: bool(done))
        assert op.get_value() is None


def test_resubmitting_an_in_flight_operation_does_not_corrupt_context(backend):
    """Resubmitting an Operation that's already in_progress must be a
    silent no-op (not counted, not re-queued) rather than corrupting
    anything about which Context it's tied to - previously (thread_aio.c)
    the C struct's ctx pointer was overwritten unconditionally for every
    argument before the in_progress check, with no matching incref, on
    whichever Context received the resubmission attempt. python_aio's
    plain-Python in-flight guard has nothing analogous to corrupt, but the
    observable contract (only ctx_a's submit counts, only its callback
    fires) must still hold identically."""
    with tempfile.NamedTemporaryFile() as f:
        ctx_a = backend.Context(max_requests=32)
        ctx_b = backend.Context(max_requests=32)

        results = []
        op = backend.Operation.write(b"x", f.fileno(), 0)
        op.set_callback(results.append)

        submitted_a = ctx_a.submit(op)
        submitted_b = ctx_b.submit(op)  # op is in_progress - must be a no-op

        assert submitted_a == 1
        assert submitted_b == 0

        assert wait_until(ctx_a, lambda: bool(results))
        assert results == [1]

    del ctx_a, ctx_b
    gc.collect()


def test_large_batch_submit_does_not_crash(backend):
    """A big enough batch used to overflow a caller-controlled-size stack
    VLA in thread_aio.c/linux_aio.c/linux_uring.c. Not a C-specific concern
    by nature (large batches should just work everywhere), so this runs
    against all backends, including python_aio."""
    with tempfile.NamedTemporaryFile() as f:
        ctx = backend.Context(max_requests=8192)
        results = []
        ops = []
        for _ in range(4000):
            op = backend.Operation.fsync(f.fileno())
            op.set_callback(results.append)
            ops.append(op)

        submitted = ctx.submit(*ops)
        assert submitted == 4000
        assert wait_until(ctx, lambda: len(results) >= 4000, timeout=15.0)


def test_context_teardown_drains_queued_operations(pooled_backend):
    """Dropping a Context with operations still queued must not abandon
    them (silently leaking their references) - it should drain the queue
    (graceful threadpool shutdown) so every already in-flight callback
    still fires. Scoped to pooled_backend (thread_aio/python_aio): only a
    real worker-thread pool has a "queued but not yet dispatched" state
    distinct from "accepted" - linux_aio/linux_uring have no such queue."""
    with tempfile.NamedTemporaryFile() as f:
        ctx = pooled_backend.Context(max_requests=256, pool_size=2)
        results = []
        ops = []
        for _ in range(64):
            op = pooled_backend.Operation.fsync(f.fileno())
            op.set_callback(results.append)
            ops.append(op)

        ctx.submit(*ops)
        del ctx
        gc.collect()

        deadline = time.monotonic() + 10.0
        while len(results) < 64 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(results) >= 64, (
            f"only {len(results)}/64 callbacks fired after teardown"
        )


def test_bad_args_do_not_accumulate_leaked_operations(backend):
    """Operation.read/write/fsync/fdsync used to `return NULL` on a bad
    argument without Py_DECREF-ing the object tp_alloc had already handed
    them - leaking it forever (unreachable from Python, but never freed).
    There's no live reference to take a weakref of in this scenario (the
    object never successfully escapes the failing constructor), so the
    only way to actually observe a per-call leak is total live-object
    count not growing with iteration count. Covers all 4 constructors,
    each run enough times that a real per-call leak would show up as
    unmistakable linear growth. Runs on every backend, including
    python_aio: its Operation.__init__ validates fd/nbytes/offset/priority
    the same way (operator.index(), matching PyArg_ParseTupleAndKeywords'
    own __index__-based coercion), raising the same TypeError synchronously
    rather than storing a bad value and failing later."""
    with tempfile.NamedTemporaryFile() as f:
        for ctor, args in [
            (backend.Operation.read, ("not-an-int", f.fileno(), 0)),
            (backend.Operation.write, (b"x", "not-an-int", 0)),
            (backend.Operation.fsync, ("not-an-int",)),
            (backend.Operation.fdsync, ("not-an-int",)),
        ]:
            gc.collect()
            before = len(gc.get_objects())

            for _ in range(500):
                try:
                    ctor(*args)
                except TypeError:
                    pass

            gc.collect()
            after = len(gc.get_objects())
            # Generous slack for unrelated interpreter/test-harness churn -
            # a real per-call leak of 500 Operation objects would blow far
            # past this.
            assert after - before < 100, (
                f"{ctor.__qualname__}: object count grew by {after - before} "
                "over 500 failed constructions - looks like a per-call leak"
            )


def test_write_with_non_bytes_payload_does_not_corrupt_its_refcount(backend):
    """A non-bytes write payload must be rejected with ValueError,
    without corrupting the caller's own reference to it, on every backend.
    The 3 C backends used to store the "O"-parsed payload_bytes argument (a
    borrowed reference) directly into the C struct's own buffer field
    before validating it was actually bytes - on the ensuing type-check
    failure, cleaning up the partially-constructed Operation decremented a
    reference it never owned, over-decrementing the caller's own object.
    python_aio can't have that exact bug (no manual refcounting to get
    wrong), but validates the same way and the observable contract - raise
    before construction completes, no lingering reference - must hold
    identically."""
    payload = ["not", "bytes"]
    before = sys.getrefcount(payload)

    with pytest.raises(ValueError):
        backend.Operation.write(payload, 0, 0)

    gc.collect()
    assert sys.getrefcount(payload) == before
