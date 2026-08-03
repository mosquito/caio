"""
Windows-specific behavior of thread_aio's CRT interop
(src/thread_aio/platform_io.rs). Everything here is skipped outright on
non-Windows platforms.
"""
import ctypes
import os
import sys
import threading

import pytest


def test_invalid_parameter_handler_is_not_process_wide(tmp_path):
    """The MSVC CRT invalid-parameter handler thread_aio installs around
    `_get_osfhandle` (needed so a closed/bogus fd raises a catchable
    error instead of aborting the process) must be scoped to the calling
    thread only, and restored immediately after - it must never replace
    the process-*wide* handler, which could otherwise silently change CRT
    error behavior for every other thread/extension in the process for as
    long as caio is imported.

    Installs a custom process-wide handler via ctypes first (simulating
    "some other code in the process already set one up"), then runs a
    normal thread_aio operation, then checks - still via ctypes - that the
    process-wide handler slot is untouched.
    """
    if sys.platform != "win32":
        pytest.skip("Windows-only (thread_aio's CRT invalid-parameter-handler path)")

    from caio import thread_aio

    if thread_aio is None:
        pytest.skip("thread_aio backend not available on this platform")

    handler_t = ctypes.CFUNCTYPE(
        None, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_size_t,
    )
    # The function lives in the modern Universal CRT (ucrtbase.dll), not the
    # legacy msvcrt.dll - matching what the Rust side actually links against
    # via a plain `extern "C"` declaration on current MSVC toolchains.
    ucrt = ctypes.CDLL("ucrtbase")
    ucrt._set_invalid_parameter_handler.restype = handler_t
    ucrt._set_invalid_parameter_handler.argtypes = [handler_t]

    @handler_t
    def my_handler(expression, function, file, line, reserved):
        pass

    previous = ucrt._set_invalid_parameter_handler(my_handler)
    try:
        path = tmp_path / "data.bin"
        path.write_bytes(b"hello")
        fd = os.open(str(path), os.O_RDONLY)
        try:
            ctx = thread_aio.Context(max_requests=4)
            op = thread_aio.Operation.read(5, fd, 0)
            done = threading.Event()
            op.set_callback(lambda _r: done.set())
            ctx.submit(op)
            assert done.wait(timeout=5.0), "operation never completed"
            assert bytes(op.get_value()) == b"hello"
        finally:
            os.close(fd)

        # Read back the CURRENT process-wide handler (re-installing
        # my_handler as a side effect, which is fine - we restore the real
        # previous one in `finally` regardless).
        current = ucrt._set_invalid_parameter_handler(my_handler)
        current_ptr = ctypes.cast(current, ctypes.c_void_p).value
        my_handler_ptr = ctypes.cast(my_handler, ctypes.c_void_p).value
        assert current_ptr == my_handler_ptr, (
            "a thread_aio operation must not replace the process-wide invalid-parameter handler"
        )
    finally:
        ucrt._set_invalid_parameter_handler(previous)
