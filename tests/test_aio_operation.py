import asyncio
import os

from linux_aio import AIOContext, EventFD, AIOOperation


def test_aio_operation(tmp_path):
    ctx = AIOContext()

    efd1 = EventFD()
    efd2 = EventFD()

    loop = asyncio.get_event_loop()
    results = asyncio.Queue()

    def on_read(efd):
        results.put_nowait((efd.fileno, efd.read()))

    loop.add_reader(efd1.fileno, on_read, efd1)
    loop.add_reader(efd2.fileno, on_read, efd2)

    async def run():
        with open(os.path.join(tmp_path, "temp.bin"), "wb+") as fp:
            fd = fp.fileno()

            op = AIOOperation.write(b"Hello world", fd, 0)
            assert op.submit(ctx, efd1) == 1

            assert op.context == ctx
            assert op.eventfd == efd1

            assert (await results.get())[0] == efd1.fileno

            op = AIOOperation.fdsync(fd)
            assert op.submit(ctx, efd1) == 1

            assert (await results.get())[0] == efd1.fileno

            assert fp.read() == b"Hello world"
            fp.seek(0)

            op1 = AIOOperation.write(b"Hello from ", fd, 0)
            op2 = AIOOperation.write(b"async world", fd, 11)
            op1.prepare(efd1)
            op2.prepare(efd2)

            assert ctx.submit(op1, op2) == 2

            res = [
                (await results.get())[0],
                (await results.get())[0],
            ]

            assert sorted(res) == sorted([efd1.fileno, efd2.fileno])

            op = AIOOperation.fdsync(fd)
            assert op.submit(ctx, efd1) == 1

            assert (await results.get())[0] == efd1.fileno

            assert fp.read() == b"Hello from async world"

            assert op1.eventfd == efd1
            assert op2.eventfd == efd2

            assert op1.context == op2.context == ctx

            op = AIOOperation.read(255, fd, 0)
            op.submit(ctx, efd1)

            res = await results.get()
            assert res[0] == efd1.fileno

            ctx.process_events()

            assert op.get_value() == b"Hello from async world"

    loop.run_until_complete(asyncio.wait_for(run(), 1))
    loop.remove_reader(efd1.fileno)
    loop.remove_reader(efd2.fileno)
