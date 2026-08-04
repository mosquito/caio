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
