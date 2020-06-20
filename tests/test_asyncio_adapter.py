import asyncio
import hashlib

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

            await asyncio.gather(
                *[context.write(part, fd, len(part) * i) for i in range(1024)]
            )

            await context.fdsync(fd)

            data = await context.read(1024 * len(part), fd, 0) == part * 1024
            assert data

            expected_hash = '93b885adfe0da089cdf634904fd59f71'
            assert hashlib.md5(bytes(data)).hexdigest() == expected_hash



@aiomisc.timeout(3)
async def test_bad_file_descritor(tmp_path, async_context_maker):
    async with async_context_maker() as context:
        with open(str(tmp_path / "temp.bin"), "wb+") as fp:
            fd = fp.fileno()

        with pytest.raises((SystemError, OSError)):
            assert await context.read(1, fd, 0) == b""

        with pytest.raises((SystemError, OSError)):
            assert await context.write(b"hello", fd, 0)
