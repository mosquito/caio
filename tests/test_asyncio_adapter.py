import asyncio
import hashlib
import os
from unittest.mock import Mock

import aiomisc
import pytest


@aiomisc.timeout(5)
async def test_linux_uring_asyncio_forwards_context_kwargs():
    uring_asyncio = pytest.importorskip("caio.linux_uring_asyncio")

    async with uring_asyncio.AsyncioContext(
        max_requests=8,
        sqpoll=True,
        deferred=True,
    ) as context:
        # Unsupported kernels/permissions may make the C context fall back
        # to a regular ring. Reaching this point proves that the asyncio
        # adapter forwarded the backend-specific option instead of rejecting
        # it in _create_context().
        assert context.context.sqpoll in (0, 1)
        assert context.deferred is True


@aiomisc.timeout(5)
async def test_adapter(tmp_path, async_context):
    context = async_context
    with open(str(tmp_path / "temp.bin"), "wb+") as fp:  # noqa: ASYNC230 (brief sync setup, not the operation under test)
        fd = fp.fileno()

        assert await context.read(32, fd, 0) == b""
        s = b"Hello world"
        assert await context.write(s, fd, 0) == len(s)
        assert await context.read(32, fd, 0) == s

        s = b"Hello real world"
        assert await context.write(s, fd, 0) == len(s)
        assert await context.read(32, fd, 0) == s

        part = b"\x00\x01\x02\x03"
        limit = 32
        expected_hash = hashlib.md5(part * limit).hexdigest()

        await asyncio.gather(
            *[context.write(part, fd, len(part) * i) for i in range(limit)]
        )

        await context.fdsync(fd)

        data = await context.read(limit * len(part), fd, 0)
        assert data == part * limit

        assert hashlib.md5(bytes(data)).hexdigest() == expected_hash


@aiomisc.timeout(3)
async def test_bad_file_descritor(tmp_path, async_context):
    context = async_context
    with open(str(tmp_path / "temp.bin"), "wb+") as fp:  # noqa: ASYNC230 (brief sync setup, not the operation under test)
        fd = fp.fileno()

    with pytest.raises((SystemError, OSError, AssertionError, ValueError)):
        assert await context.read(1, fd, 0) == b""

    with pytest.raises((SystemError, OSError, AssertionError, ValueError)):
        assert await context.write(b"hello", fd, 0)


@pytest.fixture
async def asyncio_exception_handler():
    handler = Mock(
        side_effect=lambda _loop, ctx: _loop.default_exception_handler(ctx)
    )
    event_loop = asyncio.get_running_loop()
    current_handler = event_loop.get_exception_handler()
    event_loop.set_exception_handler(handler=handler)
    yield handler
    event_loop.set_exception_handler(current_handler)


@aiomisc.timeout(3)
async def test_operations_cancel_cleanly(
    tmp_path, async_context, asyncio_exception_handler
):
    context = async_context
    with open(str(tmp_path / "temp.bin"), "wb+") as fp:  # noqa: ASYNC230 (brief sync setup, not the operation under test)
        fd = fp.fileno()

        await context.write(b"\x00", fd, 1024**2 - 1)
        assert os.stat(fd).st_size == 1024**2

        for _ in range(50):
            reads = [
                asyncio.create_task(context.read(2**16, fd, 2**16 * i))
                for i in range(16)
            ]
            _, pending = await asyncio.wait(
                reads, return_when=asyncio.FIRST_COMPLETED
            )
            for read in pending:
                read.cancel()
            if pending:
                await asyncio.wait(pending)
            asyncio_exception_handler.assert_not_called()


@aiomisc.timeout(3)
async def test_write_operations_cancel_cleanly(
    tmp_path, async_context, asyncio_exception_handler
):
    """Mirrors test_operations_cancel_cleanly but for writes - cancellation
    handling shouldn't be read-specific."""
    context = async_context
    with open(str(tmp_path / "temp.bin"), "wb+") as fp:  # noqa: ASYNC230 (brief sync setup, not the operation under test)
        fd = fp.fileno()

        for _ in range(50):
            writes = [
                asyncio.create_task(
                    context.write(b"\x01" * 2**16, fd, 2**16 * i),
                )
                for i in range(16)
            ]
            _, pending = await asyncio.wait(
                writes, return_when=asyncio.FIRST_COMPLETED
            )
            for write in pending:
                write.cancel()
            if pending:
                await asyncio.wait(pending)
            asyncio_exception_handler.assert_not_called()


@aiomisc.timeout(3)
async def test_cancel_before_first_step_runs(tmp_path, async_context, asyncio_exception_handler):
    """Cancelling right after the op's own first step (submit queued, still
    suspended at `await future`) - covers context.cancel() raising ValueError
    for an op the backend never actually got to submit to the kernel yet."""
    context = async_context
    with open(str(tmp_path / "temp.bin"), "wb+") as fp:  # noqa: ASYNC230
        fd = fp.fileno()
        task = asyncio.ensure_future(context.write(b"x", fd, 0))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        asyncio_exception_handler.assert_not_called()


@aiomisc.timeout(5)
async def test_zero_byte_read_and_write(tmp_path, async_context):
    context = async_context
    with open(str(tmp_path / "temp.bin"), "wb+") as fp:  # noqa: ASYNC230 (brief sync setup, not the operation under test)
        fd = fp.fileno()

        assert await context.write(b"", fd, 0) == 0
        assert await context.read(0, fd, 0) == b""


@aiomisc.timeout(5)
async def test_partial_read_at_eof(tmp_path, async_context):
    """Requesting more bytes than the file actually has must return exactly
    what's there, not garbage/padding out to the requested size - this
    exercises the "kernel filled less than the whole buffer" slow path
    that the fast, no-copy path (used when the buffer is filled
    completely) doesn't."""
    context = async_context
    with open(str(tmp_path / "temp.bin"), "wb+") as fp:  # noqa: ASYNC230 (brief sync setup, not the operation under test)
        fd = fp.fileno()

        payload = b"hello"
        assert await context.write(payload, fd, 0) == len(payload)

        data = await context.read(len(payload) * 20, fd, 0)
        assert data == payload
        assert len(data) == len(payload)


@aiomisc.timeout(5)
async def test_fsync_and_fdsync(tmp_path, async_context):
    context = async_context
    with open(str(tmp_path / "temp.bin"), "wb+") as fp:  # noqa: ASYNC230 (brief sync setup, not the operation under test)
        fd = fp.fileno()

        await context.write(b"data", fd, 0)
        # Return value not asserted - None on some backends, b"" on others
        # (python_aio), both meaningless here.
        await context.fsync(fd)
        await context.fdsync(fd)


@aiomisc.timeout(15)
async def test_large_transfer(tmp_path, async_context):
    context = async_context
    with open(str(tmp_path / "temp.bin"), "wb+") as fp:  # noqa: ASYNC230 (brief sync setup, not the operation under test)
        fd = fp.fileno()

        payload = os.urandom(4 * 1024 * 1024)
        expected_hash = hashlib.sha256(payload).hexdigest()

        written = await context.write(payload, fd, 0)
        assert written == len(payload)

        data = await context.read(len(payload), fd, 0)
        assert len(data) == len(payload)
        assert hashlib.sha256(bytes(data)).hexdigest() == expected_hash


@aiomisc.timeout(5)
async def test_write_extends_file_sparsely(tmp_path, async_context):
    context = async_context
    with open(str(tmp_path / "temp.bin"), "wb+") as fp:  # noqa: ASYNC230 (brief sync setup, not the operation under test)
        fd = fp.fileno()

        hole_size = 4 * 1024 * 1024
        await context.write(b"\x01", fd, hole_size)
        assert os.stat(fd).st_size == hole_size + 1

        hole = await context.read(hole_size, fd, 0)
        assert hole == b"\x00" * hole_size


@aiomisc.timeout(10)
async def test_max_requests_backpressure(tmp_path, async_context_maker):
    """A tiny max_requests must still let far more concurrent operations
    complete correctly - the asyncio-level semaphore is responsible for
    never handing the backend more in-flight operations than it was
    configured for, so this must not surface a "queue full"/"ring full"
    error from the backend itself."""
    async with async_context_maker(max_requests=2) as context:
        with open(str(tmp_path / "temp.bin"), "wb+") as fp:  # noqa: ASYNC230 (brief sync setup, not the operation under test)
            fd = fp.fileno()

            chunk = 4096
            count = 40
            expected = [bytes([i % 256]) * chunk for i in range(count)]

            await asyncio.gather(
                *[
                    context.write(expected[i], fd, i * chunk)
                    for i in range(count)
                ]
            )

            results = await asyncio.gather(
                *[context.read(chunk, fd, i * chunk) for i in range(count)]
            )
            assert results == expected


@aiomisc.timeout(10)
async def test_concurrent_non_overlapping_chunks(tmp_path, async_context):
    """Writes distinct, non-overlapping regions concurrently, then reads
    them back concurrently - if any backend's buffer handling ever aliased
    two in-flight operations' memory (e.g. a zero-copy fast path reused
    somewhere it shouldn't), this would show up as data from the wrong
    chunk appearing in the wrong place."""
    context = async_context
    with open(str(tmp_path / "temp.bin"), "wb+") as fp:  # noqa: ASYNC230 (brief sync setup, not the operation under test)
        fd = fp.fileno()

        chunk = 8192
        count = 32
        expected = [
            bytes([(i * 7 + 3) % 256]) * chunk for i in range(count)
        ]

        await asyncio.gather(
            *[
                context.write(expected[i], fd, i * chunk)
                for i in range(count)
            ]
        )

        results = await asyncio.gather(
            *[context.read(chunk, fd, i * chunk) for i in range(count)]
        )

        for i, (got, want) in enumerate(zip(results, expected)):
            assert got == want, f"chunk {i} mismatch"
