import asyncio
import logging

from . import _aio


class AIOContext:
    def __init__(self, max_requests=512, loop=None):
        self.context = _aio.Context(max_requests=max_requests)
        self.loop = loop or asyncio.get_event_loop()
        self.loop.add_reader(self.context.fileno, self._on_read_event)

        self.operations = asyncio.Queue(maxsize=max_requests)
        self.runner_task = self.loop.create_task(self._run())

    async def close(self):
        self.runner_task.cancel()

        try:
            await self.runner_task
        except asyncio.CancelledError:
            return

    def _on_read_event(self):
        while self.context.process_events():
            try:
                self.context.read()
            except BlockingIOError:
                return

    async def submit(self, op: _aio.Operation):
        if not isinstance(op, _aio.Operation):
            raise ValueError("Invalid operation object")

        return await self.operations.put(op)

    async def _run(self):
        while True:
            operations = []

            op = await self.operations.get()
            operations.append(op)

            while True:
                try:
                    operations.append(self.operations.get_nowait())
                except asyncio.QueueEmpty:
                    break

            result = self.context.submit(*operations)
            if len(operations) > result:
                logging.warning("Operation list truncated")
