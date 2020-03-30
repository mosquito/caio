import asyncio
import os
import time

from caio.thread_aio_asyncio import AsyncioContext


loop = asyncio.get_event_loop()


chunk_size = 512  # * 1024
context_max_requests = 16


async def read_file(ctx: AsyncioContext, file_id):
    offset = 0
    fname = f"data/{file_id}.bin"
    file_size = os.stat(fname).st_size

    with open(fname, "rb") as fp:
        fd = fp.fileno()

        c = 0
        futures = []
        while offset < file_size:
            futures.append(ctx.read(chunk_size, fd, offset))
            offset += chunk_size
            c += 1

        await asyncio.gather(*futures)

    return c


async def timer(future):
    await asyncio.sleep(0)
    delta = time.monotonic()
    return await future, time.monotonic() - delta


async def main():
    print("files   nr      min   madian      max   op/s    total  #ops chunk")
    for generation in range(1, 129):
        context = AsyncioContext(context_max_requests)

        futures = []

        for file_id in range(generation):
            futures.append(read_file(context, file_id))

        stat = []
        total = -time.monotonic()
        nops = 0

        for ops, delta in await asyncio.gather(*map(timer, futures)):
            stat.append(delta)
            nops += ops

        total += time.monotonic()

        stat = sorted(stat)

        ops_sec = nops / total

        dmin = stat[0]
        dmedian = stat[int(len(stat) / 2)]
        dmax = stat[-1]

        print(
            "%5d %4d %2.6f %2.6f %2.6f %6d %-3.6f %5d %d"
            % (
                generation,
                context_max_requests,
                dmin,
                dmedian,
                dmax,
                ops_sec,
                total,
                nops,
                chunk_size,
            )
        )

        await context.close()


if __name__ == "__main__":
    loop.run_until_complete(main())
