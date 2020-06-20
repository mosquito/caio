import asyncio
import abc
import logging
from typing import Awaitable
from contextlib import suppress
from functools import partial


class AsyncioContextBase(abc.ABC):
    MAX_REQUESTS_DEFAULT = 512
    CONTEXT_CLASS = None
    OPERATION_CLASS = None

    def __init__(self, max_requests=MAX_REQUESTS_DEFAULT, loop=None):
        self.loop = loop or asyncio.get_event_loop()
        self.semaphore = asyncio.Semaphore(max_requests)

        self.operations = asyncio.Queue()
        self.context = self._create_context(max_requests)

        self.runner_task = self.loop.create_task(self._run())

    def _create_context(self, max_requests):
        return self.CONTEXT_CLASS(max_requests=max_requests)

    def _destroy_context(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self):
        self.runner_task.cancel()
        self._destroy_context()
        await asyncio.gather(
            self.runner_task, return_exceptions=True
        )

    async def submit(self, op: OPERATION_CLASS):
        if not isinstance(op, self.OPERATION_CLASS):
            raise ValueError("Operation object expected")

        future = self.loop.create_future()
        self.operations.put_nowait((op, future))
        await future
        return op.get_value()

    def _on_done(self, future, result):
        """
        In general case impossible predict current thread and the thread
        of event loop. So have to use `call_soon_threadsave` the result setter.
        """
        self.loop.call_soon_threadsafe(self.semaphore.release)
        self.loop.call_soon_threadsafe(future.set_result, True)

    async def _submit_atempt(self):
        requests = {}

        op, future = await self.operations.get()
        op.set_callback(partial(self._on_done, future))
        requests[op] = future

        await self.semaphore.acquire()

        while not self.semaphore.locked():
            try:
                op, future = self.operations.get_nowait()
                op.set_callback(partial(self._on_done, future))
                requests[op] = future
                self.operations.task_done()
            except asyncio.QueueEmpty:
                break

            await self.semaphore.acquire()

        try:
            # Trying to send bulk
            self.context.submit(*requests.keys())
        except:
            # Retry send one by one and handle errors
            for request, future in requests.items():
                if future.done():
                    # Do not send twice
                    continue

                try:
                    self.context.submit(request)
                except Exception as e:
                    future.set_exception(e)

    async def _run(self):
        while True:
            try:
                await self._submit_atempt()
            except asyncio.CancelledError:
                raise
            except:
                logging.exception("Error when submitting Operations")

    def read(self, nbytes: int, fd: int, offset: int) -> Awaitable[bytes]:
        return self.submit(self.OPERATION_CLASS.read(nbytes, fd, offset))

    def write(self, payload: bytes, fd: int, offset: int) -> Awaitable[int]:
        return self.submit(self.OPERATION_CLASS.write(payload, fd, offset))

    def fsync(self, fd: int) -> Awaitable:
        return self.submit(self.OPERATION_CLASS.fsync(fd))

    def fdsync(self, fd: int) -> Awaitable:
        return self.submit(self.OPERATION_CLASS.fdsync(fd))
