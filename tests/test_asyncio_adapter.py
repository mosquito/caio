import asyncio
import hashlib
import os
from unittest.mock import Mock

import aiomisc
import pytest


@aiomisc.timeout(5)
async def test_adapter(tmp_path, async_context_maker):
    async with async_context_maker() as context:
        with open(str(tmp_path / "temp.bin"), "wb+") as fp:
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
async def test_bad_file_descritor(tmp_path, async_context_maker):
    async with async_context_maker() as context:
        with open(str(tmp_path / "temp.bin"), "wb+") as fp:
            fd = fp.fileno()

        with pytest.raises((SystemError, OSError, AssertionError, ValueError)):
            assert await context.read(1, fd, 0) == b""

        with pytest.raises((SystemError, OSError, AssertionError, ValueError)):
            assert await context.write(b"hello", fd, 0)


@pytest.fixture
async def asyncio_exception_handler(event_loop):
    handler = Mock(
        side_effect=lambda _loop, ctx: _loop.default_exception_handler(ctx)
    )
    current_handler = event_loop.get_exception_handler()
    event_loop.set_exception_handler(handler=handler)
    yield handler
    event_loop.set_exception_handler(current_handler)


@aiomisc.timeout(3)
async def test_operations_cancel_cleanly(
    tmp_path, async_context_maker, asyncio_exception_handler
):
    async with async_context_maker() as context:
        with open(str(tmp_path / "temp.bin"), "wb+") as fp:
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
