import asyncio

from benchmark_write_common import main
from caio.python_aio_asyncio import AsyncioContext


if __name__ == "__main__":
    asyncio.run(main(AsyncioContext))
