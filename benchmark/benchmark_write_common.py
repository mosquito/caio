import asyncio
import os
import time
import typing
from tempfile import NamedTemporaryFile

from caio.asyncio_base import AsyncioContextBase


data = os.urandom(1024)


async def main(context_maker: typing.Type[AsyncioContextBase]):
    async with context_maker() as context:
        with NamedTemporaryFile(mode="wb+") as fp:

            async def writer(offset=0):
                timer = - time.monotonic()
                fileno = fp.file.fileno()

                futures = []
                for i in range(1, 2 ** 15):
                    futures.append(
                        context.write(data, fileno, offset * i * len(data)),
                    )

                await asyncio.gather(*futures)
                timer += time.monotonic()
                print("Done", timer)

                return timer

            timers = []
            for i in range(10):
                timers.append(await writer(i))

            print(sum(timers) / len(timers))
