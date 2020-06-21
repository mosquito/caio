import asyncio
import os
import time
from functools import lru_cache
from caio.asyncio_base import AsyncioContextBase


chunk_size = 32  # 1024
context_max_requests = 512


@lru_cache(1024)
def open_file_by_id(file_id):
    fname = f"data/{file_id}.bin"
    return open(fname, "rb"), os.stat(fname).st_size


async def read_file(ctx: AsyncioContextBase, file_id):
    offset = 0

    fp, file_size = open_file_by_id(file_id)
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


async def main(context_maker):
    print("files   nr      min   madian      max   op/s    total  #ops chunk")

    for generation in range(1, 129):
        context = context_maker(context_max_requests)

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

        context.close()
