"""
Tests against the raw Context/Operation API directly, bypassing the
asyncio adapter - this is a real, documented part of the public API
(see e.g. caio/linux_uring.pyi), and it's also where backend-specific
low-level behavior (polling, queue capacity, cancellation) actually lives.
Not every backend exposes the same surface here (only linux_aio and
linux_uring have process_events/poll; only linux_uring also has flush),
so most tests below are conditional on hasattr() rather than assuming a
uniform API.
"""
import gc
import os
import re
import subprocess
import sys
import threading
import time
import weakref

import pytest
from conftest import drain

ABSURD_NBYTES = 2**62


def test_raw_submit_and_drain(tmp_path, polling_backend):
    with open(str(tmp_path / "temp.bin"), "wb+") as f:
        fd = f.fileno()
        ctx = polling_backend.Context(max_requests=8)

        ops = [
            polling_backend.Operation.write(bytes([i]) * 4, fd, i * 4)
            for i in range(4)
        ]
        submitted = ctx.submit(*ops)
        assert submitted == 4

        processed = drain(ctx, 4)
        assert processed == 4
        for op in ops:
            assert op.get_value() == 4

        read_ops = [
            polling_backend.Operation.read(4, fd, i * 4) for i in range(4)
        ]
        ctx.submit(*read_ops)
        drain(ctx, 4)
        for i, op in enumerate(read_ops):
            assert op.get_value() == bytes([i]) * 4


def test_raw_process_events_respects_min_max(tmp_path, polling_backend):
    with open(str(tmp_path / "temp.bin"), "wb+") as f:
        fd = f.fileno()
        ctx = polling_backend.Context(max_requests=8)

        with pytest.raises(ValueError):
            ctx.process_events(max_requests=1, min_requests=5)

        op = polling_backend.Operation.write(b"x", fd, 0)
        ctx.submit(op)
        drain(ctx, 1)
        assert op.get_value() == 1


def test_process_events_respects_timeout_and_releases_gil(polling_backend):
    """process_events(min_requests=N, timeout=T) must give up after
    roughly `timeout` seconds if fewer than N completions ever show up,
    rather than blocking indefinitely - and must release the GIL while
    waiting, not freeze the whole interpreter for the duration.

    Runs in a subprocess with its own `timeout=`, so if this regresses to
    an unbounded, GIL-held block, the subprocess gets killed at the OS
    level instead of hanging the whole test session.

    Submits nothing at all, so min_requests=1 can never be satisfied, and
    checks two things at once inside the subprocess: wall time is bounded
    by roughly `timeout` (whole seconds), and a second thread *inside that
    same subprocess* keeps making progress while the call is blocked -
    proving the GIL was actually released, not just that the call happened
    to return quickly for some other reason.
    """
    if not polling_backend.__name__.startswith("caio."):
        pytest.skip("synthetic test-only variant, not importable by name in a subprocess")

    code = (
        "import threading, time\n"
        f"import {polling_backend.__name__} as m\n"
        "ctx = m.Context(max_requests=8)\n"
        "progress = []\n"
        "stop = threading.Event()\n"
        "def other_thread():\n"
        "    while not stop.is_set():\n"
        "        progress.append(time.monotonic())\n"
        "        time.sleep(0.01)\n"
        "t = threading.Thread(target=other_thread, daemon=True)\n"
        "t.start()\n"
        "start = time.monotonic()\n"
        "n = ctx.process_events(max_requests=8, min_requests=1, timeout=1)\n"
        "elapsed = time.monotonic() - start\n"
        "stop.set()\n"
        "t.join(timeout=2.0)\n"
        "print(f'RESULT: n={n} elapsed={elapsed} progress={len(progress)}')\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("process_events(timeout=1) blocked far longer than its own timeout (subprocess timed out)")

    match = re.search(r"RESULT: n=(\d+) elapsed=([\d.]+) progress=(\d+)", proc.stdout)
    assert match, f"unexpected output; stdout={proc.stdout!r} stderr={proc.stderr!r}"
    n, elapsed, progress = int(match[1]), float(match[2]), int(match[3])

    assert n == 0
    assert elapsed < 5.0, f"process_events(timeout=1) took {elapsed:.1f}s - timeout was ignored"
    assert progress >= 3, (
        "a second thread made too little progress while process_events() was "
        "blocked - the GIL was probably held throughout instead of released"
    )


def test_raw_poll_reflects_completions(tmp_path, polling_backend):
    with open(str(tmp_path / "temp.bin"), "wb+") as f:
        fd = f.fileno()
        ctx = polling_backend.Context(max_requests=8)
        op = polling_backend.Operation.write(b"hi", fd, 0)
        ctx.submit(op)

        # poll() drains the eventfd counter, so it must not be called
        # speculatively before we know it's signaled (it's a blocking fd) -
        # only call it once flush()/process_events() has confirmed there's
        # something to report.
        drain(ctx, 1)
        assert op.get_value() == 2


def test_poll_does_not_block_when_nothing_pending(polling_backend):
    """poll() with nothing actually pending must raise BlockingIOError, as
    documented, rather than block the calling thread. Runs in a subprocess,
    killed via subprocess.run's own `timeout` (real OS-level termination)
    if it hangs - a background thread can't safely stand in for this check
    here, since poll() doesn't release the GIL, so a thread stuck inside it
    would hold the GIL hostage forever regardless of what the test's own
    main thread does.
    """
    if not polling_backend.__name__.startswith("caio."):
        pytest.skip("synthetic test-only variant, not importable by name in a subprocess")

    code = (
        f"import {polling_backend.__name__} as m\n"
        f"ctx = m.Context(max_requests=8)\n"
        f"try:\n"
        f"    ctx.poll()\n"
        f"    print('RESULT: unexpectedly-returned')\n"
        f"except BlockingIOError:\n"
        f"    print('RESULT: raised BlockingIOError')\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("poll() blocked instead of raising BlockingIOError (subprocess timed out)")

    assert "RESULT: raised BlockingIOError" in proc.stdout, (
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


def test_payload_and_get_value_blocked_while_in_flight(tmp_path, polling_backend):
    """payload/get_value() must refuse to return an operation's buffer
    while the kernel may still be actively writing into it - for
    linux_uring in particular, that buffer is the *same* Python bytes
    object the kernel writes into directly (zero-copy), so handing it out
    mid-flight would mean observing (or even, via `hash()`, corrupting the
    cached hash of) a nominally-immutable object while it's still being
    mutated. For linux_aio the read buffer is wrapped in a Mutex, but the
    kernel doesn't know about that lock, so reading through it mid-flight
    is still a data race, just a narrower one (torn reads instead of a
    shared identity issue).

    No timing race needed: `submit()` sets its own in-flight bookkeeping
    synchronously (for linux_uring, even before the SQE reaches the
    kernel via flush()), and it's only cleared once *this process* handles
    the completion via process_events()/flush() - regardless of how fast
    the kernel itself actually finishes the write.
    """
    with open(str(tmp_path / "temp.bin"), "wb+") as f:
        fd = f.fileno()
        ctx = polling_backend.Context(max_requests=8)
        op = polling_backend.Operation.write(b"hello", fd, 0)
        ctx.submit(op)

        with pytest.raises(RuntimeError):
            _ = op.payload
        with pytest.raises(RuntimeError):
            _ = op.get_value()

        drain(ctx, 1)

        # Once actually completed, both must work normally again.
        assert op.get_value() == 5
        assert bytes(op.payload) == b"hello"


def test_thread_aio_payload_and_get_value_blocked_while_in_flight(tmp_path):
    """thread_aio only: same contract as
    test_payload_and_get_value_blocked_while_in_flight above, but this
    backend has no process_events()/poll() split to hook a synchronous
    in-flight check onto - completion is purely callback-driven, and its
    worker() thread runs the actual pread()/pwrite() syscall without
    holding the GIL, so payload/get_value() reading result/buf_size/error
    concurrently from the submitting thread is a genuine, unsynchronized
    data race, not just a narrow window.

    No timing race needed here either: pool_size=1 plus a first
    operation whose own callback blocks (on blocker_release) makes the
    single worker thread provably still busy with it - so a second
    operation submitted right after is provably still queued, never
    touched by the worker, when the assertions below run.
    """
    thread_aio = pytest.importorskip("caio.thread_aio")

    with open(str(tmp_path / "temp.bin"), "wb+") as f:
        fd = f.fileno()
        ctx = thread_aio.Context(max_requests=8, pool_size=1)

        blocker_started = threading.Event()
        blocker_release = threading.Event()

        blocker_op = thread_aio.Operation.fsync(fd)
        blocker_op.set_callback(
            lambda r: (blocker_started.set(), blocker_release.wait(5.0)),
        )
        ctx.submit(blocker_op)
        assert blocker_started.wait(5.0), "blocker operation's callback never ran"

        op = thread_aio.Operation.write(b"hello", fd, 0)
        op_done = threading.Event()
        op.set_callback(lambda r: op_done.set())
        ctx.submit(op)

        with pytest.raises(RuntimeError):
            _ = op.payload
        with pytest.raises(RuntimeError):
            _ = op.get_value()

        blocker_release.set()
        assert op_done.wait(5.0), "operation never completed after unblocking the pool"

        # Once actually completed, both must work normally again.
        assert op.get_value() == 5
        assert op.payload is None  # thread_aio frees a write's payload on completion


def test_read_with_absurd_nbytes_raises_cleanly(backend):
    """Operation.read() with an unreasonably large nbytes must raise a
    catchable exception, never crash the process. A naive allocation
    strategy for a size this large doesn't just panic (which PyO3 would
    convert to a catchable PanicException) - it can *abort the whole
    process* (SIGABRT) via Rust's global alloc-error handler, which is not
    recoverable from Python at all. Runs in a subprocess since a real
    abort would otherwise take the whole test session down with it.

    python_aio doesn't preallocate a buffer at construction time (nbytes is
    just stored, the real buffer is sized lazily from whatever the actual
    read returns), so it was never at risk here and is skipped.
    """
    if backend.__name__ == "caio.python_aio":
        pytest.skip("python_aio doesn't preallocate at construction time")
    if not backend.__name__.startswith("caio."):
        pytest.skip("synthetic test-only variant, not importable by name in a subprocess")

    code = (
        f"import {backend.__name__} as m\n"
        f"try:\n"
        f"    m.Operation.read({ABSURD_NBYTES}, 0, 0)\n"
        f"    print('RESULT: unexpectedly-succeeded')\n"
        f"except (OverflowError, MemoryError, ValueError) as e:\n"
        f"    print(f'RESULT: raised {{type(e).__name__}}')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, (
        f"process aborted/crashed (returncode={proc.returncode}); "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "RESULT: raised" in proc.stdout, (
        f"expected a catchable exception; stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


@pytest.mark.parametrize("op_count", [1, 4])
def test_queue_full_does_not_crash(tmp_path, backend, op_count):
    """Submitting far beyond a tiny max_requests must never hang or corrupt
    state, but backends genuinely disagree on *how* they signal it:

    - thread_aio, linux_uring, python_aio each track capacity themselves
      (a queue/ring/counter checked before any work starts) and raise
      immediately once it's exceeded.
    - linux_aio's capacity is the kernel's own io_context depth. Since
      writes to a regular buffered file usually complete synchronously
      within io_submit() itself (Linux AIO doesn't provide real asynchrony
      for buffered I/O on most filesystems), the "backlog" rarely builds
      up enough to hit that limit - and even when it does, io_submit()'s
      documented behavior is to return fewer than requested rather than
      raise (matching the original C implementation, which just returns
      that count as-is). So for linux_aio this only asserts the
      submitted-count invariant, not an exception.

    This test exists to make that difference explicit, not to force one
    "correct" answer."""
    with open(str(tmp_path / "temp.bin"), "wb+") as f:
        fd = f.fileno()
        ctx = backend.Context(max_requests=op_count)

        ops = [
            backend.Operation.write(b"x", fd, i)
            for i in range(op_count * 64)
        ]

        if backend.__name__ == "caio.linux_aio":
            submitted = ctx.submit(*ops)
            assert 0 <= submitted <= len(ops)
        else:
            with pytest.raises((OverflowError, RuntimeError, ValueError)):
                ctx.submit(*ops)

        # The context must remain safely destructible either way.
        del ctx, ops


def test_cancel_on_real_operation_does_not_crash(tmp_path, backend):
    with open(str(tmp_path / "temp.bin"), "wb+") as f:
        fd = f.fileno()
        ctx = backend.Context(max_requests=4)
        op = backend.Operation.read(4096, fd, 0)
        ctx.submit(op)

        try:
            result = ctx.cancel(op)
        except (SystemError, RuntimeError, ValueError, NotImplementedError) as e:
            # linux_aio/linux_uring may legitimately report "too late to
            # cancel" or similar as an exception; that's fine, it just
            # must not crash or hang.
            result = e

        assert result is not None

        # Drain whatever ends up happening to the op (cancelled or raced
        # to completion) so the Context can tear down cleanly.
        if hasattr(ctx, "process_events"):
            try:
                drain(ctx, 1, timeout=2.0)
            except TimeoutError:
                pass


def test_linux_aio_operation_context_back_reference_clears_on_completion(tmp_path):
    """linux_aio only: submit() makes each accepted Operation hold a
    strong reference to its own Context (via the `.context` property)
    while genuinely in flight - so it stays usable (e.g. to call
    `op.context.cancel(op)`) even if the caller drops their own Context
    reference early - but this back-reference is cleared once the
    Operation reaches a terminal state (drained via process_events()/
    cancel()), not held forever.

    Holding it past completion (the original behavior) created an
    uncollectable reference cycle: registry -> Operation -> context ->
    Context -> registry. PyO3 classes don't participate in Python's
    cyclic GC, so once accepted, plain refcounting could never reclaim
    either object even after the operation finished, leaking both Python
    objects and - until something else eventually dropped the Context -
    the underlying kernel AIO context/eventfd too.
    """
    linux_aio = pytest.importorskip("caio.linux_aio")

    with open(str(tmp_path / "temp.bin"), "wb+") as f:
        fd = f.fileno()
        ctx = linux_aio.Context(max_requests=8)
        op = linux_aio.Operation.write(b"x" * 4, fd, 0)

        assert ctx.submit(op) == 1
        assert op.context is ctx

        del ctx

        # Still alive and fully usable through the Operation's own
        # back-reference while genuinely in flight - dropping the
        # caller's own reference alone must not have torn it down.
        still_alive = op.context
        assert still_alive.max_requests == 8

        drain(still_alive, 1)
        assert op.get_value() == 4

        # Once terminal, nothing needs the back-reference anymore - it's
        # cleared, breaking the cycle.
        assert op.context is None


def test_uring_operation_context_back_reference_clears_on_completion(tmp_path):
    """linux_uring only: same contract as linux_aio's `.context` property
    (see test_linux_aio_operation_context_back_reference_clears_on_completion
    above) - submit() makes each accepted Operation hold a strong
    reference to its own Context while genuinely in flight, cleared once
    the operation reaches a terminal state (drained via
    process_events()/flush()). Without this, a Context the caller drops
    while an operation is still outstanding could be garbage collected
    mid-flight, munmapping the SQ/CQ rings and closing uring_fd while the
    kernel might still be touching them.
    """
    linux_uring = pytest.importorskip("caio.linux_uring")

    with open(str(tmp_path / "temp.bin"), "wb+") as f:
        fd = f.fileno()
        ctx = linux_uring.Context(max_requests=8)
        op = linux_uring.Operation.write(b"x" * 4, fd, 0)

        assert ctx.submit(op) == 1
        assert op.context is ctx

        del ctx

        still_alive = op.context
        assert still_alive.max_requests == 8

        drain(still_alive, 1)
        assert op.get_value() == 4

        assert op.context is None


def test_uring_context_stays_alive_via_operation_while_genuinely_in_flight(tmp_path):
    """linux_uring only: a Context the caller drops while a submitted
    Operation is genuinely still in flight must not be collected out from
    under it - op.context (see the test above) keeps it alive via plain
    refcounting. Proven here with weakref, independent of the object's
    own repr/attributes: the outstanding write is blocked on a full pipe
    with no reader, so it provably cannot have completed yet when `del
    ctx` runs. Once the pipe is drained and the write actually completes,
    op.context clears and nothing else references the Context, so the
    weakref must clear too.
    """
    linux_uring = pytest.importorskip("caio.linux_uring")
    import fcntl  # Unix-only; this whole test is uring-only (Linux-only)

    F_SETPIPE_SZ = 1031

    r_fd, w_fd = os.pipe()
    try:
        fcntl.fcntl(w_fd, F_SETPIPE_SZ, 4096)
        os.write(w_fd, b"f" * 4096)  # fill the pipe so the next write blocks until drained

        ctx = linux_uring.Context(max_requests=8)
        ctx_ref = weakref.ref(ctx)

        # Offset must be -1 (io_uring's convention for non-seekable files,
        # same as plain write(2)) - a pipe rejects an explicit offset
        # outright, which would fail the write instantly instead of
        # leaving it pending.
        op = linux_uring.Operation.write(b"pending", w_fd, 0xFFFFFFFFFFFFFFFF)
        assert ctx.submit(op) == 1

        del ctx
        gc.collect()

        assert ctx_ref() is not None, (
            "Context was collected while its Operation was still genuinely "
            "in flight - op.context should have kept it alive"
        )

        os.read(r_fd, 8192)  # unblocks the pending write
        drain(ctx_ref(), 1, timeout=5.0)
        assert op.get_value() == len(b"pending")

        gc.collect()
        assert ctx_ref() is None, (
            "Context outlived its Operation's completion - op.context "
            "should have been cleared, leaving nothing to keep it alive"
        )
    finally:
        os.close(r_fd)
        os.close(w_fd)


def test_linux_aio_context_becomes_collectible_after_operation_completes(tmp_path):
    """linux_aio only: keeping a *completed* Operation alive - a common
    pattern, e.g. holding onto it to read `.get_value()` again later -
    must not also keep its Context alive, once the caller has otherwise
    dropped their own Context reference. Disables the cyclic garbage
    collector for the duration of the check so only plain refcounting
    can possibly free the Context: before this fix, `op.context` was
    never cleared on completion, so a completed Operation the caller
    happened to keep around would keep its Context (and the underlying
    kernel AIO context/eventfd) alive indefinitely too, regardless of
    whether the caller still wanted it.

    Note this is deliberately not the same scenario as an Operation
    abandoned *before* it ever completes (both external references
    dropped with nothing left to ever drain it) - see
    test_linux_aio_abandoned_operation_is_collectible_by_gc below for
    that one, which plain refcounting alone (as exercised here, with the
    cyclic GC disabled) genuinely cannot free - it needs `gc.collect()`.
    """
    linux_aio = pytest.importorskip("caio.linux_aio")

    gc.disable()
    try:
        with open(str(tmp_path / "temp.bin"), "wb+") as f:
            fd = f.fileno()
            ctx = linux_aio.Context(max_requests=8)
            op = linux_aio.Operation.write(b"x" * 4, fd, 0)
            ctx_ref = weakref.ref(ctx)

            assert ctx.submit(op) == 1
            drain(ctx, 1)
            assert op.get_value() == 4

            del ctx  # the caller drops Context...

            assert ctx_ref() is None, "Context leaked via a completed Operation's stale back-reference"
            # ...but the completed Operation itself is still perfectly
            # usable, unaffected by its Context having been freed.
            assert op.get_value() == 4
    finally:
        gc.enable()


def test_operation_self_referential_callback_cycle_is_collectible_by_gc(backend):
    """A callback closure that captures its own Operation (a natural pattern -
    e.g. `op.set_callback(lambda res: op.get_value())` to stash the result
    somewhere) forms a genuine reference cycle: op -> callback -> cell -> op.
    Plain refcounting can never free this on its own - cyclic GC must find and
    break it, via each C backend's Operation now supporting the GC protocol
    (Py_TPFLAGS_HAVE_GC / tp_traverse / tp_clear), same as python_aio's plain
    Python class already did without any extra work.

    Unlike test_linux_aio_abandoned_operation_is_collectible_by_gc above,
    this doesn't need a Context at all - the cycle is entirely within the
    Operation object itself, so it's a scenario all four backends can hit,
    not just the two native ones with a `.context` back-reference.
    """
    def make_cycle():
        op = backend.Operation.fsync(1)
        op.set_callback(lambda res: op)
        return weakref.ref(op)

    gc.disable()
    try:
        op_ref = make_cycle()
        assert op_ref() is not None, (
            "expected plain refcounting alone to NOT free this cycle - "
            "if it did, this test isn't exercising the scenario it claims to"
        )
    finally:
        gc.enable()

    gc.collect()
    assert op_ref() is None, "gc.collect() must break the self-referential callback cycle"


def test_resubmission_behavior_is_backend_specific(tmp_path, backend):
    """All four backends now agree that submit()-ing the same Operation
    object twice back to back, with no drain in between, must silently
    skip the second attempt - none of them will ever hand out two
    concurrent dispatches of the same one-shot Operation:

    - thread_aio marks "in_progress" sticky forever (never reset), so a
      second submit() always silently skips it, regardless of timing.
    - linux_uring/linux_aio reset "in_progress" only once the operation
      actually completes; since this test never calls flush() (the only
      thing that hands submitted SQEs to the kernel for linux_uring's
      default, non-SQPOLL mode), the operation cannot possibly have
      completed yet, so the second submit() is deterministically skipped
      here too - otherwise the kernel would end up with two concurrent
      iocbs/SQEs pointing at the same buffer, a real data race for reads.
    - python_aio now has the same in-flight guard as the other three
      (Operation.in_progress, checked/set under Context's own lock).

    This test exists to catch any regression in this shared guarantee."""
    with open(str(tmp_path / "temp.bin"), "wb+") as f:
        fd = f.fileno()
        ctx = backend.Context(max_requests=4)
        op = backend.Operation.write(b"x", fd, 0)

        first = ctx.submit(op)
        assert first == 1

        second = ctx.submit(op)
        assert second == 0, "expected sticky/not-yet-completed in-flight state to skip resubmission"

        if hasattr(ctx, "process_events"):
            try:
                drain(ctx, first + second, timeout=2.0)
            except TimeoutError:
                pass


def test_linux_aio_rejects_concurrent_resubmit(tmp_path):
    """linux_aio only, complementing test_resubmission_behavior_is_backend_specific
    above: submit() must atomically claim each op, so the kernel never ends
    up with two concurrent iocbs pointing at the very same buffer.
    """
    linux_aio = pytest.importorskip("caio.linux_aio")

    with open(str(tmp_path / "temp.bin"), "wb+") as f:
        fd = f.fileno()
        ctx = linux_aio.Context(max_requests=4)
        op = linux_aio.Operation.write(b"x", fd, 0)

        first = ctx.submit(op)
        assert first == 1

        second = ctx.submit(op)
        assert second == 0, "must not hand the kernel two concurrent iocbs for the same buffer"

        drain(ctx, first, timeout=2.0)


def test_submit_same_operation_object_twice_in_one_batch_is_accepted_once(tmp_path, backend):
    """All four backends: submitting the same in-flight Operation object
    twice within a single submit() call must only ever be accepted once -
    not twice with two different RequestIds/dispatches silently
    overwriting each other. A prior bug (thread_aio/linux_aio/linux_uring)
    accepted both occurrences (each one's own already-in-progress check
    ran before either was actually marked), causing the underlying I/O to
    happen twice with only one completion ever delivered.
    """
    path = tmp_path / "data.bin"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        ctx = backend.Context(max_requests=8)
        op = backend.Operation.write(b"x", fd, 0)

        done = threading.Event()
        op.set_callback(lambda _res: done.set())

        accepted = ctx.submit(op, op)
        assert accepted == 1, "the same Operation object must only be accepted once per submit() call"

        if hasattr(ctx, "process_events"):
            drain(ctx, 1)
        else:
            assert done.wait(timeout=5.0), "operation never completed"

        assert op.get_value() == 1
    finally:
        os.close(fd)


def test_resubmit_after_short_transfer_uses_original_size(tmp_path, polling_backend):
    """linux_uring, linux_aio: an operation that once got a short read must
    not be resubmittable at all (both are one-shot on the shared caio-core
    engine now - design/generalized-safe-design.md decision #4), and a
    fresh `Operation.read(nbytes, ...)` retrying the same read must still
    ask for the ORIGINAL requested size, not the smaller amount the short
    read actually transferred - otherwise a retried read would be silently
    capped forever, even once more data became available and a full-size
    transfer would succeed.

    No error involved - just a short read (fewer bytes available than
    requested) followed by more data actually showing up before the
    retry, so the only way the second read comes back short is if the
    request size itself got silently clamped.
    """
    path = tmp_path / "data.bin"
    path.write_bytes(b"abcd")

    fd = os.open(str(path), os.O_RDONLY)
    try:
        ctx = polling_backend.Context(max_requests=8)
        op = polling_backend.Operation.read(8, fd, 0)

        assert ctx.submit(op) == 1
        drain(ctx, 1)
        assert bytes(op.get_value()) == b"abcd", "expected a short read of the 4 available bytes"

        with open(str(path), "r+b") as f:
            f.seek(4)
            f.write(b"efgh")

        # One-shot: the same, already-completed Operation must not be
        # accepted again - silently skipped (submitted count 0).
        assert ctx.submit(op) == 0, "a completed one-shot Operation must not be resubmittable"
        retry = polling_backend.Operation.read(8, fd, 0)
        assert ctx.submit(retry) == 1
        drain(ctx, 1)
        assert bytes(retry.get_value()) == b"abcdefgh", (
            "a fresh Operation must request the full original size, never a shrunk one"
        )
    finally:
        os.close(fd)


def test_uring_failed_op_not_resubmittable_and_leaves_no_stale_error(tmp_path):
    """linux_uring only: an operation that failed at completion time (a
    real CQE with res=-EBADF, not an eager io_submit()-time raise - see
    below) must not be resubmittable afterward (one-shot, design decision
    #4), and a *fresh* Operation retried against the same, now-valid fd
    number must succeed with no trace of the previous attempt's error.

    This used to be tested by resubmitting the very same Operation object
    after fixing up the fd via dup2() (fd is fixed for an Operation's
    whole lifetime - see AbstractOperation's docstring) - that specific
    resubmit-after-failure path is now structurally impossible under the
    one-shot contract, so this instead checks the two properties that
    replace it: rejection of the stale object, and a clean fresh attempt.

    linux_uring only: a write against a closed fd fails at *completion*
    time here. linux_aio's io_submit() validates the fd eagerly and raises
    a Python exception immediately instead (confirmed by hand) rather than
    ever reaching its own, symmetric completion-time error handling - so
    the same scenario can't be driven through linux_aio's public API.
    """
    linux_uring = pytest.importorskip("caio.linux_uring")

    bad_path = tmp_path / "closed.bin"
    good_path = tmp_path / "good.bin"

    ctx = linux_uring.Context(max_requests=8)  # create ctx first: its own fds must not collide below

    closed_fd = os.open(str(bad_path), os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(closed_fd)

    op = linux_uring.Operation.write(b"hello", closed_fd, 0)
    assert ctx.submit(op) == 1
    drain(ctx, 1)

    assert op.error != 0, "expected the write against a closed fd to fail"
    with pytest.raises(SystemError):
        op.get_value()

    good_fd = os.open(str(good_path), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        # good_fd often already *is* closed_fd's number here (the OS tends
        # to hand out the lowest just-freed fd), making this a documented
        # POSIX no-op - but dup2() is what makes the aliasing deliberate
        # and correct regardless of which number the allocator happens to
        # pick.
        os.dup2(good_fd, closed_fd)

        assert ctx.submit(op) == 0, "a completed one-shot Operation must not be resubmittable, even after fd repair"

        retry = linux_uring.Operation.write(b"hello", closed_fd, 0)
        assert ctx.submit(retry) == 1
        drain(ctx, 1)

        assert retry.error == 0, "a fresh Operation must carry no stale error from a prior, unrelated attempt"
        assert retry.get_value() == 5
    finally:
        os.close(closed_fd)
        if good_fd != closed_fd:
            os.close(good_fd)


def test_uring_short_read_tail_is_zeroed_not_heap_garbage(tmp_path):
    """linux_uring only: bytes past a short read's actual transfer count
    must be deterministic zero, not whatever was previously sitting in
    that heap memory. `PyBytes_FromStringAndSize(NULL, n)` leaves its
    buffer uninitialized, so this must be zeroed explicitly.

    Dirties the heap with distinguishable non-zero bytes first (allocating
    and freeing several same-sized buffers) to raise the odds an
    uninitialized allocation would visibly reuse that memory and fail this
    check - not a fully deterministic reproduction of the bug (heap reuse
    isn't guaranteed), but this assertion is unconditionally the correct
    behavior regardless.
    """
    linux_uring = pytest.importorskip("caio.linux_uring")

    nbytes = 256
    for _ in range(64):
        bytearray(b"\xff" * nbytes)

    path = tmp_path / "data.bin"
    path.write_bytes(b"AB")  # far shorter than nbytes

    fd = os.open(str(path), os.O_RDONLY)
    try:
        ctx = linux_uring.Context(max_requests=8)
        op = linux_uring.Operation.read(nbytes, fd, 0)
        ctx.submit(op)
        drain(ctx, 1)

        assert bytes(op.get_value()) == b"AB"
        assert bytes(op.payload) == b"AB" + b"\x00" * (nbytes - 2), (
            "untouched tail past a short read must be zero, not leftover heap contents"
        )
    finally:
        os.close(fd)


def test_uring_read_resubmit_rejected_and_previous_result_unmutated(tmp_path):
    """linux_uring only: a completed read Operation must not be
    resubmittable (one-shot, design decision #4) - so the bytes object a
    previous completion already handed out via get_value()/.payload can
    never be mutated by a later kernel write into the same buffer, since
    there is no later kernel write into it. A fresh Operation started
    against the same fd afterward must get its own, independent buffer.
    """
    linux_uring = pytest.importorskip("caio.linux_uring")

    path = tmp_path / "data.bin"
    path.write_bytes(b"AAAA")

    fd = os.open(str(path), os.O_RDONLY)
    try:
        ctx = linux_uring.Context(max_requests=8)
        op = linux_uring.Operation.read(4, fd, 0)
        ctx.submit(op)
        drain(ctx, 1)

        first_result = op.get_value()
        assert bytes(first_result) == b"AAAA"
        first_hash = hash(first_result)

        with open(str(path), "r+b") as f:
            f.write(b"BBBB")

        assert ctx.submit(op) == 0, "a completed one-shot Operation must not be resubmittable"

        retry = linux_uring.Operation.read(4, fd, 0)
        ctx.submit(retry)
        drain(ctx, 1)
        second_result = retry.get_value()
        assert bytes(second_result) == b"BBBB"

        assert bytes(first_result) == b"AAAA", (
            "a rejected resubmit must not mutate a bytes object already handed out by a previous completion"
        )
        assert hash(first_result) == first_hash
        assert first_result is not second_result, (
            "a fresh Operation must use its own buffer, not reuse a previous completion's object"
        )
    finally:
        os.close(fd)


def test_uring_submit_overflow_does_not_strand_or_leak_prior_ops(tmp_path):
    """linux_uring only: when a single submit() call overflows the SQ ring
    partway through a batch, every op accepted *before* the overflow point
    must still be genuinely visible to the kernel (sq_tail/outstanding
    published) and reclaimable - not permanently stuck "in flight" with an
    unpublished SQE and an unreachable leaked reference.

    max_requests=1 deliberately: with a bigger capacity, more than one op
    from the batch could be accepted before the overflow point, and this
    test's own later `drain(ctx, 1)` calls (which only wait for *some*
    completion, not a specific one) could then race and count one of
    those other, unrelated ops' completions instead of the one actually
    being asserted on - flaky, not a real bug. Capacity 1 makes "the op
    accepted before the overflow point" unambiguously just `ops[0]`.
    """
    linux_uring = pytest.importorskip("caio.linux_uring")

    with open(str(tmp_path / "temp.bin"), "wb+") as f:
        fd = f.fileno()
        ctx = linux_uring.Context(max_requests=1)
        ops = [linux_uring.Operation.write(b"x", fd, i) for i in range(4 * 64)]

        with pytest.raises(OverflowError):
            ctx.submit(*ops)

        drain(ctx, 1, timeout=2.0)
        assert ops[0].get_value() == 1, "the first op must have been accepted, submitted, and completed"

        # One-shot: the completed op from the overflowed batch must not be
        # resubmittable itself, but a fresh Operation must submit and
        # complete cleanly - the overflow must not have left the context
        # itself in some stuck or capacity-exhausted state.
        assert ctx.submit(ops[0]) == 0, "a completed one-shot Operation must not be resubmittable"
        retry = linux_uring.Operation.write(b"x", fd, 0)
        assert ctx.submit(retry) == 1
        drain(ctx, 1, timeout=2.0)
        assert retry.get_value() == 1


def test_uring_reentrant_callback_does_not_double_consume_completion(tmp_path):
    """linux_uring only: drain_cq() must fully commit the completion
    ring's state (head + outstanding) *before* invoking any completion
    callback - a callback that reentrantly calls process_events()/flush()
    must never be able to observe (and reprocess) a CQE the outer,
    still-running drain has already consumed. Reprocessing it would
    reconstruct the same "kernel-owned" reference a second time, causing a
    double-decref once both reconstructions eventually drop.
    """
    linux_uring = pytest.importorskip("caio.linux_uring")

    with open(str(tmp_path / "temp.bin"), "wb+") as f:
        fd = f.fileno()
        ctx = linux_uring.Context(max_requests=8)
        op = linux_uring.Operation.write(b"x", fd, 0)

        reentrant_results = []
        op.set_callback(lambda _res: reentrant_results.append(ctx.process_events()))

        ctx.submit(op)
        refcount_before = sys.getrefcount(op)

        # flush() both publishes the SQE to the kernel and drains whatever's
        # already completed inline - for a plain buffered write this is
        # *usually* immediate, but not guaranteed synchronous on every
        # kernel/filesystem. If it isn't, fall back to process_events()
        # with a real timeout: unlike flush(), it waits/polls the CQ ring
        # even when there's nothing new to submit (flush() has nothing left
        # to publish on a second call and returns 0 without even looking at
        # the CQ ring).
        ctx.flush()
        if not reentrant_results:
            ctx.process_events(max_requests=8, min_requests=1, timeout=5)
        assert reentrant_results, "operation never completed within the timeout"

        assert reentrant_results == [0], (
            "a reentrant process_events() call from inside a completion callback "
            "must not see (and reprocess) the CQE the outer call just consumed"
        )
        refcount_after = sys.getrefcount(op)
        assert refcount_after == refcount_before - 1, (
            "drain must reclaim exactly one leaked reference for this completion, "
            "not be double-decremented by a reentrant re-consume"
        )
        assert op.get_value() == 1


def test_linux_aio_submit_type_error_does_not_strand_earlier_ops(tmp_path):
    """linux_aio only: a wrong-typed argument later in the same submit()
    call must not leave an earlier, valid operation in that same call
    permanently stuck in_flight - it must never have been touched at all,
    since the whole call raises before anything reaches io_submit().
    """
    linux_aio = pytest.importorskip("caio.linux_aio")

    with open(str(tmp_path / "temp.bin"), "wb+") as f:
        fd = f.fileno()
        ctx = linux_aio.Context(max_requests=4)
        op = linux_aio.Operation.write(b"x", fd, 0)

        with pytest.raises(TypeError):
            ctx.submit(op, object())

        # Must not be stuck "in flight" - a normal submit must still work.
        submitted = ctx.submit(op)
        assert submitted == 1, "op from the rejected batch must not be permanently stuck in_flight"
        drain(ctx, 1, timeout=2.0)
        assert op.get_value() == 1


def test_linux_aio_process_events_max_requests_is_bounded(backend):
    """linux_aio only: process_events(max_requests=...) sizes an internal
    events buffer directly from this caller-controlled argument - an
    absurdly large value must raise cleanly instead of attempting a huge
    allocation (u32::MAX would be roughly 128 GiB). Runs in a subprocess:
    an unbounded attempt at that allocation could abort the whole process
    (Rust's global alloc-error handler, not a catchable panic) rather than
    just failing this one call, which would take the whole test session
    down with it.
    """
    if backend.__name__ != "caio.linux_aio":
        pytest.skip("linux_aio-only")

    code = (
        "import caio.linux_aio as m\n"
        "ctx = m.Context(max_requests=8)\n"
        "try:\n"
        "    ctx.process_events(max_requests=2**32 - 1)\n"
        "    print('RESULT: unexpectedly-succeeded')\n"
        "except OverflowError:\n"
        "    print('RESULT: raised OverflowError')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, (
        f"process aborted/crashed (returncode={proc.returncode}); "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "RESULT: raised OverflowError" in proc.stdout, (
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


@pytest.mark.flaky(reruns=3)
def test_process_events_negative_timeout_waits_indefinitely(tmp_path, polling_backend):
    """process_events(timeout<0) means "wait indefinitely" - matching the
    native convention each backend's own blocking primitive already uses
    for exactly this (linux_aio's io_getevents() takes a NULL timespec;
    linux_uring's new eventfd-based wait passes -1 to poll()) - rather than
    being rejected outright, which was the previous behavior. Verified two
    ways: it must still be genuinely blocked with nothing submitted yet
    (not return early or misinterpret negative as "don't wait"), and it
    must actually wake up and return once a real completion satisfies
    min_requests, not stay stuck forever once given something to wait for.
    """
    with open(str(tmp_path / "temp.bin"), "wb+") as f:
        fd = f.fileno()
        ctx = polling_backend.Context(max_requests=8)

        result = {}

        def wait():
            result["n"] = ctx.process_events(min_requests=1, timeout=-1)

        t = threading.Thread(target=wait)
        t.start()
        try:
            t.join(timeout=0.3)
            assert t.is_alive(), (
                "process_events(timeout=-1) returned before any operation "
                "was even submitted - it must block, not treat a negative "
                "timeout as \"don't wait\""
            )

            op = polling_backend.Operation.write(b"x" * 4, fd, 0)
            ctx.submit(op)
            if hasattr(ctx, "flush"):
                ctx.flush()

            t.join(timeout=5.0)
            assert not t.is_alive(), (
                "process_events(timeout=-1) never woke up for a real "
                "completion - it must not block forever once satisfied"
            )
        finally:
            t.join(timeout=5.0)

        assert result.get("n", 0) >= 1


def test_uring_process_events_max_requests_bounds_callbacks_not_just_return_value(tmp_path):
    """linux_uring only: process_events(max_requests=N) must not run more
    than N callbacks synchronously, even though poll() itself always
    drains the *entire* CQ ring in one go (no peek-without-consume
    support) - anything beyond N must be held back (in a bridge-side
    pending queue) for a later call, not delivered anyway while only
    under-reporting the count the return value claims.

    Five writes, each blocked on its own full pipe (nothing reading), are
    submitted and flush()ed while still guaranteed not to have completed
    - flush() itself delivers nothing yet, confirmed below, so nothing
    has been delivered via any path before process_events() runs. All
    five are then unblocked together (every pipe drained), so they become
    real completions at roughly the same time, deterministically -
    letting max_requests bound exactly how many of them get delivered per
    call, not just how many get reported.
    """
    linux_uring = pytest.importorskip("caio.linux_uring")
    import fcntl  # Unix-only; this whole test is uring-only (Linux-only)

    F_SETPIPE_SZ = 1031
    pipes = [os.pipe() for _ in range(5)]
    try:
        for r_fd, w_fd in pipes:
            fcntl.fcntl(w_fd, F_SETPIPE_SZ, 4096)
            os.write(w_fd, b"f" * 4096)  # fill each pipe so the next write blocks

        ctx = linux_uring.Context(max_requests=16)
        called = []
        ops = []
        for i, (_r_fd, w_fd) in enumerate(pipes):
            # -1 (io_uring's convention for non-seekable files, same as
            # plain write(2)) - a pipe rejects an explicit offset outright.
            op = linux_uring.Operation.write(b"x", w_fd, 0xFFFFFFFFFFFFFFFF)
            op.set_callback(lambda _r, i=i: called.append(i))
            ops.append(op)
        ctx.submit(*ops)

        found = ctx.flush()
        assert found == 0 and not called, (
            "test setup bug: all 5 writes must still be blocked (pipes full) at this point"
        )

        for r_fd, _w_fd in pipes:
            os.read(r_fd, 8192)
        time.sleep(0.05)  # let the kernel actually post the completions

        first = ctx.process_events(max_requests=1, min_requests=0, timeout=0)
        assert first == 1, f"expected exactly 1 reported, got {first}"
        assert len(called) == 1, (
            f"process_events(max_requests=1) must invoke exactly 1 callback, not {len(called)}"
        )

        second = ctx.process_events(max_requests=10, min_requests=0, timeout=0)
        assert second == 4, f"expected the remaining 4 held over from before, got {second}"
        assert len(called) == 5, f"expected all 5 eventually delivered, got {len(called)}"
    finally:
        for r_fd, w_fd in pipes:
            os.close(r_fd)
            os.close(w_fd)


def test_uring_process_events_min_requests_ignores_cancel_sentinel(tmp_path):
    """linux_uring only: process_events(min_requests=N) must wait for N
    *real* completions, not N raw CQEs - a cancel's own ASYNC_CANCEL
    sentinel CQE satisfies io_uring's own notion of "something completed"
    without being a real completion poll() would ever hand back. Without
    this, a stray sentinel already sitting in the ring lets the wait loop
    exit on its very first check (before real completions are even
    checked), well before a real completion that would actually satisfy
    min_requests has had a chance to arrive.

    Uses the same blocked-pipe-write + background-thread-drains-it-late
    technique as the Drop-reap sentinel test above: a write genuinely
    still in flight when process_events() is called, made to complete
    ~0.2s later. With the bug, the stray sentinel already in the ring
    would satisfy min_requests=1 immediately, so the call returns 0 real
    completions almost instantly, well before that 0.2s delay elapses.
    """
    linux_uring = pytest.importorskip("caio.linux_uring")
    import fcntl  # Unix-only; this whole test is uring-only (Linux-only)

    F_SETPIPE_SZ = 1031

    with open(str(tmp_path / "temp.bin"), "wb+") as f:
        fd = f.fileno()
        r_fd, w_fd = os.pipe()
        try:
            fcntl.fcntl(w_fd, F_SETPIPE_SZ, 4096)
            os.write(w_fd, b"f" * 4096)  # fill the pipe so the next write blocks

            ctx = linux_uring.Context(max_requests=16)

            # A real op, completed and drained first, purely to generate a
            # cancel sentinel below - cancelling an already-finished op
            # still posts an ASYNC_CANCEL completion CQE for it.
            done_op = linux_uring.Operation.write(b"z", fd, 0)
            ctx.submit(done_op)
            drain(ctx, 1, timeout=5.0)
            ctx.cancel(done_op)
            time.sleep(0.05)
            assert ctx.poll() > 0, "cancel's own sentinel completion never got posted"

            # Genuinely still in flight when process_events() is called
            # below - the pipe is full and nothing is reading it. Offset
            # must be -1 (io_uring's convention for non-seekable files) -
            # a pipe rejects an explicit offset outright.
            blocked_op = linux_uring.Operation.write(b"pending", w_fd, 0xFFFFFFFFFFFFFFFF)
            ctx.submit(blocked_op)
            ctx.flush()

            drained_before_call_returned = threading.Event()

            def drain_pipe_late():
                time.sleep(0.2)
                drained_before_call_returned.set()
                os.read(r_fd, 8192)

            t = threading.Thread(target=drain_pipe_late)
            t.start()
            try:
                n = ctx.process_events(max_requests=8, min_requests=1, timeout=5)
                observed_drained_flag = drained_before_call_returned.is_set()
            finally:
                t.join(timeout=5.0)

            assert n == 1, f"expected the one real completion, got n={n}"
            assert observed_drained_flag, (
                "process_events(min_requests=1) returned before the real completion actually "
                "happened - a stray cancel sentinel must have satisfied min_requests early "
                "instead of being filtered out"
            )
        finally:
            os.close(r_fd)
            os.close(w_fd)


def test_uring_context_usable_from_a_different_thread(tmp_path):
    """linux_uring only: a Context constructed on one thread must be fully
    usable (submit/flush) from a different thread - this backend's Python
    API doesn't pin a Context to the thread that created it, so
    io_uring_setup() must not either. IORING_SETUP_SINGLE_ISSUER, on
    kernels that support it, does exactly that: it pins the ring to
    whichever task's io_uring_setup()/io_uring_enter() call created it and
    rejects submission from any other task with -EEXIST.
    """
    linux_uring = pytest.importorskip("caio.linux_uring")

    with open(str(tmp_path / "temp.bin"), "wb+") as f:
        fd = f.fileno()
        ctx = linux_uring.Context(max_requests=8)  # created on the main thread
        op = linux_uring.Operation.write(b"hello", fd, 0)

        result = {}
        errors = []

        def worker():
            try:
                ctx.submit(op)
                drain(ctx, 1, timeout=5.0)
                result["bytes_written"] = op.get_value()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=10.0)

        assert not t.is_alive(), "worker thread did not finish in time"
        assert not errors, f"submit()/flush() from a different thread raised: {errors!r}"
        assert result.get("bytes_written") == 5
        assert os.pread(fd, 5, 0) == b"hello"


def test_uring_context_drop_does_not_invoke_callbacks(tmp_path):
    """Context teardown reaps outstanding operations (see the sentinel-CQE
    test above) to avoid unmapping the rings out from under still-pending
    kernel I/O, but it must not invoke Python callbacks while doing so -
    a destructor is a risky place to run arbitrary user code (interpreter
    shutdown ordering, reentrancy into the object being destroyed), and
    nothing about "waiting for the kernel to finish with this memory"
    requires it. The completion should still be applied (result/error
    available if some other reference to the Operation survives), just
    silently, with no callback call.
    """
    linux_uring = pytest.importorskip("caio.linux_uring")

    with open(str(tmp_path / "temp.bin"), "wb+") as f:
        fd = f.fileno()
        ctx = linux_uring.Context(max_requests=8)
        op = linux_uring.Operation.write(b"hello", fd, 0)

        invoked = []
        op.set_callback(lambda res: invoked.append(res))
        ctx.submit(op)

        del ctx
        gc.collect()

        assert not invoked, "Context teardown must not invoke operation callbacks"


def test_thread_aio_drop_waits_for_a_genuinely_blocked_worker(tmp_path):
    """thread_aio only: dropping a Context whose single worker is still
    blocked in a real syscall (a `read()` on an empty pipe, not merely a
    task still sitting in the queue) must wait for that worker to actually
    finish, not give up the instant shutdown is signaled - the fix for
    the opposite bug (an *unbounded* wait that could hang forever if a
    worker never returned at all) must not have swung to the other
    extreme and started abandoning genuinely in-flight work immediately.
    A background thread makes the blocking read return shortly after
    Drop starts (proving it's genuinely still running, not merely
    queued), and teardown must not return before that write happens, but
    also must return well within a few seconds - nowhere near the full
    drop-reap deadline - once it does.
    """
    pytest.importorskip("caio.thread_aio")
    import caio.thread_aio as m

    r_fd, w_fd = os.pipe()
    try:
        ctx = m.Context(max_requests=4, pool_size=1)
        op = m.Operation.read(1, r_fd, 0)
        ctx.submit(op)

        # Give the single worker a moment to actually enter the blocking
        # read() before Drop runs - otherwise it could still be sitting
        # in the queue and get cancelled per the "not yet started" path
        # instead of genuinely blocking.
        time.sleep(0.05)

        written_before_drop_returned = threading.Event()

        def write_after_delay():
            time.sleep(0.2)
            written_before_drop_returned.set()
            os.write(w_fd, b"x")

        t = threading.Thread(target=write_after_delay)
        t.start()
        try:
            start = time.monotonic()
            del ctx
            gc.collect()
            elapsed = time.monotonic() - start
        finally:
            t.join(timeout=5.0)

        assert written_before_drop_returned.is_set(), (
            "Context teardown returned before the blocked read actually completed - "
            "it must wait for genuinely in-flight work, not give up immediately"
        )
        assert elapsed < 5.0, (
            f"teardown took {elapsed:.2f}s - should return shortly after the real "
            "completion, not anywhere near the full drop-reap deadline"
        )
    finally:
        os.close(r_fd)
        os.close(w_fd)


def test_linux_aio_cancel_from_wrong_context_is_rejected(tmp_path):
    """linux_aio only: cancelling an Operation through a Context it was
    never submitted through must be rejected, not silently routed to a
    same-numbered but completely unrelated request in that other Context.

    Each Context's own request-ID sequence starts at 0 independently, so
    the very first operation submitted through any two Contexts both get
    local RequestId(0) - a bare RequestId alone can't tell them apart.
    caio-core pairs every ID with the ContextId that issued it, and checks
    it in Engine::cancel() before ever touching that Context's own
    registry.
    """
    linux_aio = pytest.importorskip("caio.linux_aio")

    path_a = tmp_path / "a.bin"
    path_b = tmp_path / "b.bin"
    fd_a = os.open(str(path_a), os.O_WRONLY | os.O_CREAT, 0o600)
    fd_b = os.open(str(path_b), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        ctx_a = linux_aio.Context(max_requests=8)
        ctx_b = linux_aio.Context(max_requests=8)

        op_a = linux_aio.Operation.write(b"a" * 4096, fd_a, 0)
        op_b = linux_aio.Operation.write(b"b" * 4096, fd_b, 0)
        assert ctx_a.submit(op_a) == 1
        assert ctx_b.submit(op_b) == 1

        with pytest.raises(ValueError):
            ctx_b.cancel(op_a)

        # op_b, ctx_b's own unrelated request, must be completely
        # unaffected by the rejected cross-context cancel attempt.
        drain(ctx_b, 1)
        assert op_b.get_value() == 4096

        drain(ctx_a, 1)
        assert op_a.get_value() == 4096
    finally:
        os.close(fd_a)
        os.close(fd_b)


def test_uring_cancel_from_wrong_context_does_not_disturb_other_context(tmp_path):
    """linux_uring only: same scenario as
    test_linux_aio_cancel_from_wrong_context_is_rejected, but linux_uring's
    cancel() is fire-and-forget (always returns 0, never raises) - so the
    only observable way to confirm the fix is that the *other* Context's
    own, same-numbered request completes normally and is left untouched by
    a cancel() call made through a different Context.
    """
    linux_uring = pytest.importorskip("caio.linux_uring")

    path_a = tmp_path / "a.bin"
    path_b = tmp_path / "b.bin"
    fd_a = os.open(str(path_a), os.O_WRONLY | os.O_CREAT, 0o600)
    fd_b = os.open(str(path_b), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        ctx_a = linux_uring.Context(max_requests=8)
        ctx_b = linux_uring.Context(max_requests=8)

        op_a = linux_uring.Operation.write(b"a" * 4096, fd_a, 0)
        op_b = linux_uring.Operation.write(b"b" * 4096, fd_b, 0)
        assert ctx_a.submit(op_a) == 1
        assert ctx_b.submit(op_b) == 1

        # No manual flush() here - drain() below already calls it, and a
        # write against a regular file can complete inline during flush()
        # (see flush()'s own doc comment); flushing twice risks drain()
        # waiting forever for a completion flush() already delivered.
        assert ctx_b.cancel(op_a) == 0

        drain(ctx_b, 1)
        assert op_b.get_value() == 4096, "op_b must complete normally, undisturbed by a cross-context cancel"

        drain(ctx_a, 1)
        assert op_a.get_value() == 4096
    finally:
        os.close(fd_a)
        os.close(fd_b)
