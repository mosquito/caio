import asyncio
import os
import time
import typing
from tempfile import NamedTemporaryFile

from caio.asyncio_base import AsyncioContextBase


data = os.urandom(65534)


async def main(context_maker: typing.Type[AsyncioContextBase]):
    async with context_maker() as context:
        with NamedTemporaryFile(mode="wb+") as fp:

            timer = - time.monotonic()
            fileno = fp.file.fileno()

            futures = []
            for i in range(1, 1024):
                futures.append(context.write(data, fileno, i * len(data)))

            await asyncio.gather(*futures)
            timer += time.monotonic()
            print('Done', timer)
