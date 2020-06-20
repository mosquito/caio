import asyncio

from caio.thread_aio_asyncio import AsyncioContext


async def main():
    ctx = AsyncioContext()

    with open("/tmp/test.file", "wb+") as fp:
        fp.write(b"python" * 64)

    with open("/tmp/test.file", "rb") as fp:
        print(await ctx.read(1024, fp.fileno(), 0))


asyncio.run(main())
