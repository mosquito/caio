import asyncio
import csv

import os
import time
from glob import glob
from contextlib import ExitStack
from itertools import chain

implementation = os.environ.get("IMPL")

if implementation == 'linux':
    from caio.linux_aio_asyncio import AsyncioContext
elif implementation == 'thread':
    from caio.thread_aio_asyncio import AsyncioContext
elif implementation == 'python':
    from caio.python_aio_asyncio import AsyncioContext
else:
    raise ValueError("Define IMPL environment variable")


print("Using %r" % AsyncioContext.__module__)


loop = asyncio.get_event_loop()

context_max_requests = 512


async def read_files(ctx: AsyncioContext, chunk_size, max_ops):
    futures = []

    with ExitStack() as stack:
        def operations_generator():
            for fname in glob("data/*.bin"):
                offset = 0

                file_size = os.stat(fname).st_size
                fp = stack.enter_context(open(fname, "rb"))
                fd = fp.fileno()

                while offset < file_size:
                    yield timer(ctx.read(chunk_size, fd, offset))
                    if len(futures) >= max_ops:
                        return

                    offset += chunk_size

        for operation in operations_generator():
            futures.append(operation)

        result = await asyncio.gather(*map(timer, futures))
        return len(futures), result


async def timer(future):
    await asyncio.sleep(0)
    delta = time.monotonic()
    await future
    return time.monotonic() - delta


header = "\n nr       min   median      max     op/s    total     #ops    chunk"


async def main():
    os.makedirs("results", exist_ok=True)

    print(header)

    context = AsyncioContext(context_max_requests)

    result_file = "results/%s.csv" % (
        AsyncioContext.__module__.split(".")[-1],
    )

    with open(result_file, "w") as res_fp:
        results = csv.writer(
            res_fp, delimiter=',',
            quotechar='"', quoting=csv.QUOTE_MINIMAL
        )

        results.writerow([
            "nr", "min", "median",
            "max", "ops/s", "total", "nops", "chunk_size"
        ])

        gen = chain(
            range(2 ** 15, 1024, -32),
            range(1024, 128, -4),
            range(128, 16, -2),
            range(16, 2, -1)
        )

        for chunk_size in gen:
            total = -time.monotonic()

            nops, stat = await read_files(context, chunk_size, 10000)

            total += time.monotonic()

            stat = sorted(stat)

            ops_sec = nops / total

            dmin = stat[0]
            dmedian = stat[int(len(stat) / 2)]
            dmax = stat[-1]

            print(
                "%4d %8d %8d %8d %8d %8d %8d %8d" % (
                    context_max_requests,
                    dmin * 1000000,
                    dmedian * 1000000,
                    dmax * 1000000,
                    ops_sec,
                    total * 1000000,
                    nops,
                    chunk_size,
                )
            )

            results.writerow([
                context_max_requests, dmin, dmedian,
                dmax, ops_sec, total, nops, chunk_size
            ])

        await context.close()


if __name__ == "__main__":
    loop.run_until_complete(main())
