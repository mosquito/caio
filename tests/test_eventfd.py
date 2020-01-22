import asyncio
import os

from linux_aio import EventFD


def test_eventfd():
    loop = asyncio.get_event_loop()
    efd = EventFD()
    assert efd.fileno > 0

    async def run():
        future = loop.create_future()

        def on_read(fileno):
            future.set_result(os.read(fileno, 8))
            loop.remove_reader(fileno)

        loop.add_reader(efd.fileno, on_read, efd.fileno)

        loop.call_soon(os.write, efd.fileno, b"\xfe" * 8)

        assert await future == b'\xfe' * 8

    loop.run_until_complete(asyncio.wait_for(run(), 2))
