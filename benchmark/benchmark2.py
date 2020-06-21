import asyncio
import csv

import os
import time
from glob import glob
from contextlib import ExitStack
from itertools import chain

implementation = os.environ.get("IMPL")

if implementation == "linux":
    from caio.linux_aio_asyncio import AsyncioContext
    from caio.linux_aio import Operation
elif implementation == "thread":
    from caio.thread_aio_asyncio import AsyncioContext
    from caio.thread_aio import Operation
elif implementation == "python":
    from caio.python_aio_asyncio import AsyncioContext
    from caio.python_aio import Operation
else:
    raise ValueError("Define IMPL environment variable")


print("Using %r" % AsyncioContext.__module__)


loop = asyncio.get_event_loop()

context_max_requests = 512


async def read_files(ctx: AsyncioContext, chunk_size, max_ops):
    total = 0

    with ExitStack() as stack:

        def operations_generator():
            nonlocal total

            for fname in glob("data/*.bin"):
                offset = 0

                file_size = os.stat(fname).st_size
                fp = stack.enter_context(open(fname, "rb"))
                fd = fp.fileno()

                while offset < file_size:
                    op = Operation.read(chunk_size, fd, offset)
                    yield ctx.submit(op), op

                    if total >= max_ops:
                        return

                    offset += chunk_size
                    total += 1

        results = asyncio.Queue()

        for future, operation in operations_generator():
            future.add_done_callback(lambda _: results.put_nowait(operation))

        async def waiter():
            nonlocal total, results
            count = 0

            while count < total:
                op = await results.get()
                assert op.get_value(), "Null value %r" % op.get_value()

                count += 1

        await waiter()
        return total


header = "\n nr       op/s    total     #ops    chunk"


async def main():
    os.makedirs("results", exist_ok=True)

    print(header)

    context = AsyncioContext(context_max_requests)

    result_file = "results/%s.csv" % (
        AsyncioContext.__module__.split(".")[-1],
    )

    with open(result_file, "w") as res_fp:
        results = csv.writer(
            res_fp, delimiter=",", quotechar='"', quoting=csv.QUOTE_MINIMAL
        )

        results.writerow(["nr", "ops/s", "total", "nops", "chunk_size"])

        gen = chain(
            range(2 ** 15, 1024, -2048),
            # range(1024, 128, -4),
            range(128, 16, -16),
            range(16, 2, -1),
        )

        for chunk_size in gen:
            total = -time.monotonic()

            nops = await read_files(context, chunk_size, 10000)

            total += time.monotonic()

            ops_sec = nops / total

            print(
                "%4d %8d %8d %8d %8d"
                % (
                    context_max_requests,
                    ops_sec,
                    total * 1000000,
                    nops,
                    chunk_size,
                )
            )

            results.writerow(
                [context_max_requests, ops_sec, total, nops, chunk_size]
            )

        context.close()


if __name__ == "__main__":
    loop.run_until_complete(main())
