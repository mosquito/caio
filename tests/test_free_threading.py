import os
import sys
import sysconfig
import threading

import pytest

import caio
from caio import python_aio


def test_gil_stays_disabled_when_only_python_aio_available():
    """caio ships no Py_mod_gil declaration on any C extension, so importing
    thread_aio/linux_aio/linux_uring under a free-threaded interpreter
    silently re-enables the GIL (CPython's own safety fallback). When none
    of them are importable here (e.g. built against a regular interpreter,
    then run under a free-threaded one - different SOABI, so they simply
    don't match), nothing should have flipped the GIL back on."""
    if not sysconfig.get_config_var("Py_GIL_DISABLED"):
        pytest.skip("not a free-threaded build")
    if any((caio.thread_aio, caio.linux_aio, caio.linux_uring)):
        pytest.skip("a C extension is available here too - GIL re-enable is expected")
    assert sys._is_gil_enabled() is False


def test_high_concurrency_stress_no_data_races(tmp_path):
    """Hammers python_aio's own bookkeeping (the _lock-protected
    _in_progress counter and per-operation in_progress flag) with many
    concurrent writes then reads dispatched across a real multi-worker
    ThreadPool. Under a free-threaded interpreter these workers can run
    truly in parallel instead of being serialized by the GIL, so a race in
    that bookkeeping - two workers claiming the same slot, a result
    crossing over to the wrong Operation - would show up here as a lost
    write or a chunk read back with the wrong content, not just a
    thread-timing fluke."""
    count = 200
    chunk = 4096
    path = tmp_path / "stress.bin"
    path.write_bytes(b"\x00" * (count * chunk))
    fd = os.open(str(path), os.O_RDWR)
    try:
        ctx = python_aio.Context(max_requests=count, pool_size=32)
        expected = [bytes([i % 256]) * chunk for i in range(count)]

        written = [None] * count
        lock = threading.Lock()
        remaining = [count]
        done = threading.Event()

        def make_write_cb(i):
            def cb(n):
                with lock:
                    written[i] = n
                    remaining[0] -= 1
                    if remaining[0] == 0:
                        done.set()
            return cb

        for i in range(count):
            op = python_aio.Operation.write(expected[i], fd, i * chunk)
            op.set_callback(make_write_cb(i))
            assert ctx.submit(op) == 1

        assert done.wait(10), f"only {count - remaining[0]}/{count} writes completed"
        assert written == [chunk] * count

        read_back = [None] * count
        remaining = [count]
        done = threading.Event()

        def make_read_cb(i, op):
            def cb(_n):
                with lock:
                    read_back[i] = op.get_value()
                    remaining[0] -= 1
                    if remaining[0] == 0:
                        done.set()
            return cb

        for i in range(count):
            op = python_aio.Operation.read(chunk, fd, i * chunk)
            op.set_callback(make_read_cb(i, op))
            assert ctx.submit(op) == 1

        assert done.wait(10), f"only {count - remaining[0]}/{count} reads completed"
        for i, (got, want) in enumerate(zip(read_back, expected)):
            assert got == want, f"chunk {i} mismatch"
    finally:
        os.close(fd)
        ctx.close()


def test_same_operation_is_claimed_once_across_contexts():
    """An Operation's one-shot claim must be synchronized by the Operation.

    Context._execute() currently protects ``operation.in_progress`` with the
    Context's lock.  That only serializes submissions through the *same*
    Context: two Contexts use two unrelated locks and can both observe False
    before either stores True when the GIL is disabled.

    Synchronizing each pair of submissions makes the race frequent enough to
    be a useful regression test without depending on filesystem timing.
    """
    if getattr(sys, "_is_gil_enabled", lambda: True)():
        pytest.skip("requires a free-threaded interpreter with the GIL disabled")

    iterations = 20_000
    contexts = (
        python_aio.Context(max_requests=iterations * 2),
        python_aio.Context(max_requests=iterations * 2),
    )
    start = threading.Barrier(3, timeout=30)
    finished = threading.Barrier(3, timeout=30)
    current = [None]
    submitted = [0, 0]
    errors = []

    def submitter(index, context):
        try:
            for _ in range(iterations):
                start.wait()
                submitted[index] += context.submit(current[0])
                finished.wait()
        except Exception as exc:  # noqa: BLE001 (surface child-thread failures)
            errors.append(exc)
            start.abort()
            finished.abort()

    threads = [
        threading.Thread(target=submitter, args=(index, context))
        for index, context in enumerate(contexts)
    ]

    try:
        for thread in threads:
            thread.start()

        for _ in range(iterations):
            current[0] = python_aio.Operation(
                0, None, None, python_aio.OpCode.NOOP,
            )
            start.wait()
            finished.wait()

        for thread in threads:
            thread.join()

        assert not errors
        assert sum(submitted) == iterations, (
            f"{sum(submitted) - iterations} Operations were submitted twice"
        )
    finally:
        start.abort()
        finished.abort()
        for thread in threads:
            thread.join(timeout=5)
        for context in contexts:
            context.close()
            context.pool.join()
