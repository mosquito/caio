import asyncio

from . import linux_aio


class AsyncioAIOContext:
    MAX_REQUESTS_DEFAULT = 128

    def __init__(self, max_requests=MAX_REQUESTS_DEFAULT, loop=None):
        self.context = linux_aio.Context(max_requests=max_requests)
        self.loop = loop or asyncio.get_event_loop()
        self.semaphore = asyncio.Semaphore(max_requests)

        self.loop.add_reader(self.context.fileno, self._on_read_event)

        self.operations = asyncio.Queue()
        self.runner_task = self.loop.create_task(self._run())

    def _on_read_event(self):
        self.context.poll()

        for _ in range(self.context.process_events()):
            self.semaphore.release()

    async def close(self):
        self.loop.remove_reader(self.context.fileno)
        self.runner_task.cancel()

        try:
            await self.runner_task
        except asyncio.CancelledError:
            return

    async def submit(self, op: linux_aio.Operation):
        if not isinstance(op, linux_aio.Operation):
            raise ValueError("Operation object expected")

        future = self.loop.create_future()
        op.set_callback(future.set_result)

        await self.operations.put((op, future))

        return await future

    async def _run(self):
        while True:
            op, future = await self.operations.get()

            try:
                await self.semaphore.acquire()
                self.operations.task_done()
                self.context.submit(op)
            except Exception as e:
                future.set_exception(e)

    async def read(self, nbytes: int, fd: int, offset: int) -> bytes:
        op = linux_aio.Operation.read(nbytes, fd, offset)
        await self.submit(op)
        return op.get_value()

    async def write(self, payload: bytes, fd: int, offset: int) -> int:
        return await self.submit(
            linux_aio.Operation.write(payload, fd, offset)
        )

    async def fsync(self, fd: int):
        await self.submit(linux_aio.Operation.fsync(fd))

    async def fdsync(self, fd: int):
        await self.submit(linux_aio.Operation.fdsync(fd))
