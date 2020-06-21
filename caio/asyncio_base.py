import asyncio
import abc
from typing import Awaitable
from functools import partial


class AsyncioContextBase(abc.ABC):
    MAX_REQUESTS_DEFAULT = 512
    CONTEXT_CLASS = None
    OPERATION_CLASS = None

    def __init__(self, max_requests=None, loop=None):
        self.loop = loop or asyncio.get_event_loop()
        self.semaphore = asyncio.BoundedSemaphore(
            max_requests or self.MAX_REQUESTS_DEFAULT
        )
        self.context = self._create_context(
            max_requests or self.MAX_REQUESTS_DEFAULT
        )

    def _create_context(self, max_requests):
        return self.CONTEXT_CLASS(max_requests=max_requests)

    def _destroy_context(self):
        return

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        self._destroy_context()

    async def submit(self, op: OPERATION_CLASS):
        if not isinstance(op, self.OPERATION_CLASS):
            raise ValueError("Operation object expected")

        future = self.loop.create_future()
        op.set_callback(partial(self._on_done, future))

        async with self.semaphore:
            self.context.submit(op)
            await future
            return op.get_value()

    def _on_done(self, future, result):
        """
        In general case impossible predict current thread and the thread
        of event loop. So have to use `call_soon_threadsave` the result setter.
        """
        self.loop.call_soon_threadsafe(future.set_result, True)

    def read(self, nbytes: int, fd: int, offset: int) -> Awaitable[bytes]:
        return self.submit(self.OPERATION_CLASS.read(nbytes, fd, offset))

    def write(self, payload: bytes, fd: int, offset: int) -> Awaitable[int]:
        return self.submit(self.OPERATION_CLASS.write(payload, fd, offset))

    def fsync(self, fd: int) -> Awaitable:
        return self.submit(self.OPERATION_CLASS.fsync(fd))

    def fdsync(self, fd: int) -> Awaitable:
        return self.submit(self.OPERATION_CLASS.fdsync(fd))
