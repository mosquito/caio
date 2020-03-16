import asyncio

from .thread_aio import Context, Operation


class AsyncioContext:
    MAX_REQUESTS_DEFAULT = 512

    def __init__(self, max_requests=MAX_REQUESTS_DEFAULT, loop=None):
        self.context = Context(max_requests=max_requests)
        self.loop = loop or asyncio.get_event_loop()
        self.semaphore = asyncio.Semaphore(max_requests)

    async def close(self):
        del self.context

    async def submit(self, op: Operation):
        if not isinstance(op, Operation):
            raise ValueError("Operation object expected")

        future = self.loop.create_future()
        op.set_callback(future.set_result)

        self.context.submit(op)

        return await future

    async def read(self, nbytes: int, fd: int, offset: int) -> bytes:
        op = Operation.read(nbytes, fd, offset)
        await self.submit(op)
        return op.get_value()

    async def write(self, payload: bytes, fd: int, offset: int) -> int:
        return await self.submit(
            Operation.write(payload, fd, offset)
        )

    async def fsync(self, fd: int):
        await self.submit(Operation.fsync(fd))

    async def fdsync(self, fd: int):
        await self.submit(Operation.fdsync(fd))
