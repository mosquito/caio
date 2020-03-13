import asyncio

import os
import time
from concurrent.futures.thread import ThreadPoolExecutor


loop = asyncio.get_event_loop()


chunk_size = 256 * 1024
pool_max_workers = 16


async def read_file(pool: ThreadPoolExecutor, semaphore, file_id):
    offset = 0
    fname = f"data/{file_id}.bin"
    file_size = os.stat(fname).st_size

    async def read(offset):
        def sync_read(fd):
            fd = os.dup(fd)
            os.lseek(fd, offset, 0)
            os.read(fd, chunk_size)
            os.close(fd)

        async with semaphore:
            return await loop.run_in_executor(pool, sync_read, fd)

    with open(fname, "rb") as fp:
        fd = fp.fileno()

        c = 0
        while offset < file_size:
            await read(offset)
            offset += chunk_size
            c += 1

    return c


async def timer(future):
    await asyncio.sleep(0)
    delta = time.monotonic()
    return await future, time.monotonic() - delta


async def main():
    print("files   nr      min   madian      max   op/s    total  #ops chunk")
    for generation in range(1, 129):
        pool = ThreadPoolExecutor(max_workers=pool_max_workers)
        semaphore = asyncio.Semaphore(512)

        futures = []

        for file_id in range(generation):
            futures.append(read_file(pool, semaphore, file_id))

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
                pool_max_workers,
                dmin,
                dmedian,
                dmax,
                ops_sec,
                total,
                nops,
                chunk_size,
            )
        )

        pool.shutdown(True)


if __name__ == "__main__":
    loop.run_until_complete(main())
