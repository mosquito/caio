import asyncio
import csv
import logging
import os
import sys
from contextlib import suppress
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns
from typing import Type, TypeVar

from rich.console import Console
from rich.progress import Progress

from caio.asyncio_base import AsyncioContextBase
from caio.python_aio_asyncio import AsyncioContext as PythonAsyncioContext

CONTEXTS = [PythonAsyncioContext]

with suppress(ImportError):
    from caio.linux_aio_asyncio import AsyncioContext as LinuxAsyncioContext
    CONTEXTS.append(LinuxAsyncioContext)

with suppress(ImportError):
    from caio.thread_aio_asyncio import AsyncioContext as ThreadAsyncioContext
    CONTEXTS.append(ThreadAsyncioContext)


T = TypeVar("T")
CONSOLE = Console(file=sys.stderr)


async def test(context_class: Type[AsyncioContextBase], results):
    file_size = 256 * (1024**2)
    concurrences = list(range(1, 5000, 200))[::-1]
    max_ops = [1, 4, 8, 16, 32, 64]
    chunk_size = [int(file_size / c) for c in concurrences[::5]]

    impl = str(context_class.__module__)

    with Progress(console=CONSOLE) as progress:
        for concurrency, chunk_size, ops in progress.track(
            list(product(concurrences, chunk_size, max_ops)),
            description=f'Benchmarking {impl}'
        ):
            async with context_class(64) as context:
                with TemporaryDirectory() as dirname:
                    path = Path(dirname)

                    with open(path / "test.bin", "ab+") as fp:
                        fd = fp.fileno()
                        delta = -perf_counter_ns()
                        chunk = int(file_size / concurrency)
                        data = os.urandom(chunk)
                        tasks = [
                            context.write(payload=data, fd=fd, offset=chunk * n)
                            for n in range(concurrency)
                        ]
                        try:
                            await asyncio.gather(*tasks)
                        except Exception as e:
                            results.writerow([
                                impl, "write", delta + perf_counter_ns(), chunk, concurrency, ops, False, str(e),
                            ])
                        else:
                            results.writerow([
                                impl, "write", delta + perf_counter_ns(), chunk, concurrency, ops, True, None,
                            ])

                    with open(path / "test.bin", "rb+") as fp:
                        fd = fp.fileno()
                        chunk = int(file_size / concurrency)

                        delta = -perf_counter_ns()
                        tasks = [
                            context.read(nbytes=chunk, fd=fd, offset=chunk * n)
                            for n in range(concurrency)
                        ]

                        try:
                            await asyncio.gather(*tasks)
                        except Exception as e:
                            results.writerow([
                                impl, "read", delta + perf_counter_ns(), chunk, concurrency, ops, False, str(e)
                            ])
                        else:
                            results.writerow([
                                impl, "read", delta + perf_counter_ns(), chunk, concurrency, ops, True, None
                            ])


def main():
    logging.basicConfig(level=logging.INFO)
    results = csv.writer(sys.stdout, dialect='excel')
    results.writerow([
        "implementation", "operation", "time", "chunk_size", "concurrency", "max_ops", "success", "exception"
    ])

    for context in CONTEXTS:
        print(f"Benchmarking {context!r}", file=sys.stderr)
        asyncio.run(test(context, results))


if __name__ == "__main__":
    main()
