import asyncio
import abc
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

    async def close(self):
        self.runner_task.cancel()

        try:
            await self.runner_task
        except asyncio.CancelledError:
            return
        finally:
            self._destroy_context()

    async def submit(self, op: OPERATION_CLASS):
        if not isinstance(op, self.OPERATION_CLASS):
            raise ValueError("Operation object expected")

        future = self.loop.create_future()
        self.operations.put_nowait((op, future))

        return await future

    async def _run(self):
        def on_done(future, _):
            self.loop.call_soon_threadsafe(self.semaphore.release)
            self.loop.call_soon_threadsafe(future.set_result, True)

        async def step():
            requests = []

            op, future = await self.operations.get()
            op.set_callback(partial(on_done, future))
            requests.append(op)

            await self.semaphore.acquire()

            while not self.semaphore.locked():
                try:
                    op, future = self.operations.get_nowait()
                    op.set_callback(partial(on_done, future))
                    requests.append(op)
                    self.operations.task_done()
                except asyncio.QueueEmpty:
                    break

                await self.semaphore.acquire()

            self.context.submit(*requests)

        while True:
            with suppress(Exception):
                await step()

    async def read(self, nbytes: int, fd: int, offset: int) -> bytes:
        op = self.OPERATION_CLASS.read(nbytes, fd, offset)
        await self.submit(op)
        return op.get_value()

    async def write(self, payload: bytes, fd: int, offset: int) -> int:
        return await self.submit(
            self.OPERATION_CLASS.write(payload, fd, offset)
        )

    async def fsync(self, fd: int):
        await self.submit(self.OPERATION_CLASS.fsync(fd))

    async def fdsync(self, fd: int):
        await self.submit(self.OPERATION_CLASS.fdsync(fd))

