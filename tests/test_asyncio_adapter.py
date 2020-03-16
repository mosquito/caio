import asyncio
import os

import pytest

from caio import AsyncioContext


def test_context():
    assert AsyncioContext()

    with pytest.raises(SystemError):
        assert AsyncioContext(-1)

    with pytest.raises(SystemError):
        assert AsyncioContext(65534)


def test_adapter(tmp_path):
    loop = asyncio.get_event_loop()

    async def run():
        context = AsyncioContext()
        with open(os.path.join(tmp_path, "temp.bin"), "wb+") as fp:
            fd = fp.fileno()

            assert await context.read(32, fd, 0) == b""
            assert await context.write(b"Hello world", fd, 0)
            assert await context.read(32, fd, 0) == b"Hello world"

            assert await context.write(b"Hello world", fd, 0)
            assert await context.read(32, fd, 0) == b"Hello world"

            part = b"\x00\x01\x02\x03"

            await asyncio.gather(
                *[context.write(part, fd, len(part) * i) for i in range(1024)]
            )

            await context.fdsync(fd)

            assert await context.read(1024 * len(part), fd, 0) == part * 1024

    loop.run_until_complete(asyncio.wait_for(run(), 5))


def test_bad_file_descritor(tmp_path):
    loop = asyncio.get_event_loop()

    async def run():
        context = AsyncioContext()
        with open(os.path.join(tmp_path, "temp.bin"), "wb+") as fp:
            fd = fp.fileno()

        with pytest.raises(SystemError):
            assert await context.read(32, fd, 0) == b""

    loop.run_until_complete(asyncio.wait_for(run(), 5))
