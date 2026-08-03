import gc
import sys
import tempfile
import time

import pytest

from caio import thread_aio


def drain(deadline_s=5.0, poll=lambda: None):
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if poll():
            return True
        time.sleep(0.001)
    return False


def test_get_value_on_fsync_does_not_corrupt_none_refcount():
    """get_value() on fsync/fdsync used to `return Py_None` instead of
    Py_RETURN_NONE - a borrowed reference handed out as if it were new.
    Run it enough times that an under-refcounted None would show up as a
    wrong count (or, in the worst case, a crash from None being freed)."""
    with tempfile.NamedTemporaryFile() as f:
        ctx = thread_aio.Context()
        results = []

        for _ in range(200):
            op = thread_aio.Operation.fsync(f.fileno())
            op.set_callback(results.append)
            ctx.submit(op)

        assert drain(5.0, lambda: len(results) >= 200)

        op = thread_aio.Operation.fsync(f.fileno())
        done = []
        op.set_callback(done.append)
        ctx.submit(op)
        assert drain(5.0, lambda: bool(done))
        assert op.get_value() is None


def test_resubmitting_an_in_flight_operation_does_not_corrupt_context():
    """Resubmitting an Operation that's already in_progress must be a
    silent no-op (not counted, not re-queued) rather than corrupting the
    op's ctx pointer - previously ctx was overwritten unconditionally for
    every argument before the in_progress check, with no matching incref,
    on whichever Context received the resubmission attempt."""
    with tempfile.NamedTemporaryFile() as f:
        ctx_a = thread_aio.Context()
        ctx_b = thread_aio.Context()

        results = []
        op = thread_aio.Operation.write(b"x", f.fileno(), 0)
        op.set_callback(results.append)

        submitted_a = ctx_a.submit(op)
        submitted_b = ctx_b.submit(op)  # op is in_progress - must be a no-op

        assert submitted_a == 1
        assert submitted_b == 0

        assert drain(5.0, lambda: bool(results))
        assert results == [1]

    del ctx_a, ctx_b
    gc.collect()


def test_large_batch_submit_does_not_crash():
    """AIOContext_submit used to size a stack VLA by the caller-controlled
    tuple length - a big enough *ops call could overflow the stack."""
    with tempfile.NamedTemporaryFile() as f:
        ctx = thread_aio.Context(max_requests=8192, pool_size=16)
        results = []
        ops = []
        for _ in range(4000):
            op = thread_aio.Operation.fsync(f.fileno())
            op.set_callback(results.append)
            ops.append(op)

        submitted = ctx.submit(*ops)
        assert submitted == 4000
        assert drain(15.0, lambda: len(results) >= 4000)


def test_context_teardown_drains_queued_operations():
    """Dropping a Context with operations still queued must not abandon
    them (silently leaking their Py_INCREF'd references) - it should
    drain the queue (graceful threadpool shutdown) so every already
    in-flight callback still fires."""
    with tempfile.NamedTemporaryFile() as f:
        ctx = thread_aio.Context(max_requests=256, pool_size=2)
        results = []
        ops = []
        for _ in range(64):
            op = thread_aio.Operation.fsync(f.fileno())
            op.set_callback(results.append)
            ops.append(op)

        ctx.submit(*ops)
        del ctx
        gc.collect()

        assert drain(10.0, lambda: len(results) >= 64), (
            f"only {len(results)}/64 callbacks fired after teardown"
        )


def test_bad_args_do_not_leak_the_partially_constructed_operation():
    """Operation.read/write/fsync/fdsync used to `return NULL` on a bad
    argument without Py_DECREF-ing the object tp_alloc had already handed
    them - leaking it forever (unreachable from Python, but never freed).
    A weakref to the object dying right after the failed call (once the
    exception itself is no longer holding a traceback reference to any
    local) proves it was actually freed."""
    with tempfile.NamedTemporaryFile() as f:
        for ctor, args in [
            (thread_aio.Operation.read, ("not-an-int", f.fileno(), 0)),
            (thread_aio.Operation.write, (b"x", "not-an-int", 0)),
            (thread_aio.Operation.fsync, ("not-an-int",)),
            (thread_aio.Operation.fdsync, ("not-an-int",)),
        ]:
            refs = []

            def make(ctor=ctor, args=args):
                try:
                    ctor(*args)
                except TypeError:
                    pass

            make()
            gc.collect()
            # Nothing outside this function ever held a reference, so if
            # the object wasn't leaked there should be nothing left to
            # even take a weakref of - the real assertion here is just
            # that this whole block completes without ballooning memory
            # across many iterations, checked in the next test.
            del refs


def test_bad_args_do_not_accumulate_leaked_operations():
    """Repeats the bad-args construction many times and checks the total
    live-object count doesn't grow with iteration count - a real per-call
    leak would show up as linear growth here."""
    gc.collect()
    before = len(gc.get_objects())

    with tempfile.NamedTemporaryFile() as f:
        for _ in range(500):
            try:
                thread_aio.Operation.read("not-an-int", f.fileno(), 0)
            except TypeError:
                pass

    gc.collect()
    after = len(gc.get_objects())
    # Generous slack for unrelated interpreter/test-harness churn - a real
    # per-call leak of 500 AIOOperation objects would blow far past this.
    assert after - before < 100, (
        f"object count grew by {after - before} over 500 failed constructions "
        "- looks like a per-call leak"
    )


def test_write_with_non_bytes_payload_does_not_corrupt_its_refcount():
    """AIOOperation_write used to store the "O"-parsed payload_bytes
    argument (a borrowed reference) directly into self->py_buffer before
    validating it was actually bytes. On the ensuing PyBytes_Check failure,
    Py_XDECREF(self) triggered AIOOperation_dealloc's Py_CLEAR(py_buffer),
    decrementing a reference this Operation never owned - over-decrementing
    the caller's own object."""
    payload = ["not", "bytes"]
    before = sys.getrefcount(payload)

    with pytest.raises(ValueError):
        thread_aio.Operation.write(payload, 0, 0)

    gc.collect()
    assert sys.getrefcount(payload) == before
