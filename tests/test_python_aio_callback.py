import tempfile
import threading
import time

import pytest

from caio import python_aio


def test_submit_without_callback_completes_normally():
    """set_callback() is optional - a missing callback must be a normal
    case, not something that trips up the result-handler thread."""
    with tempfile.NamedTemporaryFile() as f:
        ctx = python_aio.Context()
        op = python_aio.Operation.write(b"hello", f.fileno(), 0)
        ctx.submit(op)

        deadline = time.monotonic() + 5.0
        while ctx._in_progress and time.monotonic() < deadline:
            time.sleep(0.001)

        assert op.get_value() == 5


def test_raising_callback_does_not_kill_the_result_handler_thread():
    """A callback that raises must not take down ThreadPool's single
    result-handler thread - if it did, every operation submitted after the
    raising one would never have its result/callback delivered."""
    with tempfile.NamedTemporaryFile() as f:
        ctx = python_aio.Context()

        unraisable = []
        original_hook = threading.excepthook
        threading.excepthook = unraisable.append
        try:
            failing_op = python_aio.Operation.write(b"first", f.fileno(), 0)
            failing_op.set_callback(lambda _: 1 / 0)
            ctx.submit(failing_op)

            deadline = time.monotonic() + 5.0
            while failing_op.written == 0 and time.monotonic() < deadline:
                time.sleep(0.001)

            assert len(unraisable) == 1
            assert unraisable[0].exc_type is ZeroDivisionError

            results = []
            following_op = python_aio.Operation.write(b"second", f.fileno(), 5)
            following_op.set_callback(results.append)
            ctx.submit(following_op)

            deadline = time.monotonic() + 5.0
            while not results and time.monotonic() < deadline:
                time.sleep(0.001)

            assert results == [6]
        finally:
            threading.excepthook = original_hook


def test_max_requests_one_never_admits_a_second_concurrent_submit():
    """The capacity check used to compare with `>` instead of `>=`, so
    max_requests=1 silently admitted a second in-flight operation - simulate
    "one already in flight" directly rather than racing a real background
    job, since a real read/write completes far too fast to reliably catch
    the window."""
    ctx = python_aio.Context(max_requests=1, pool_size=1)
    ctx._in_progress = 1
    with pytest.raises(RuntimeError):
        ctx.submit(python_aio.Operation.fsync(0))


def test_bounds_are_validated_at_construction():
    with pytest.raises(ValueError):
        python_aio.Context(pool_size=0)

    with pytest.raises(ValueError):
        python_aio.Context(max_requests=0)

    with pytest.raises(ValueError):
        python_aio.Context(pool_size=python_aio.Context.MAX_POOL_SIZE)


def test_set_callback_rejects_non_callable():
    op = python_aio.Operation.fsync(0)
    with pytest.raises(ValueError):
        op.set_callback("not callable")


def test_close_is_idempotent():
    ctx = python_aio.Context()
    ctx.close()
    ctx.close()
    with pytest.raises(RuntimeError):
        ctx.submit(python_aio.Operation.fsync(0))


def test_scheduling_failure_rolls_back_the_reserved_slot():
    """If apply_async() itself raises after the capacity slot was already
    reserved (e.g. a concurrent close() tore down the underlying pool
    between the capacity check and the actual scheduling call), the slot
    must be given back - otherwise _in_progress stays permanently inflated
    even though no job was ever actually running."""
    ctx = python_aio.Context(max_requests=1)
    ctx.pool.close()  # underlying pool gone, but ctx._state is still OPEN

    with pytest.raises(Exception):  # noqa: B017 (whatever ThreadPool itself raises)
        ctx.submit(python_aio.Operation.fsync(0))

    assert ctx._in_progress == 0
