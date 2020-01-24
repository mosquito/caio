import asyncio
import os

from linux_aio._aio import Context, Operation


def test_aio_operation(tmp_path):
    ctx = Context(16)

    loop = asyncio.get_event_loop()

    def on_read():
        ctx.poll()
        ctx.process_events()

    loop.add_reader(ctx.fileno, on_read)

    async def run():
        with open(os.path.join(tmp_path, "temp.bin"), "wb+") as fp:
            fd = fp.fileno()
            futures = []
            ops = []

            for _ in range(32):
                op = Operation.write(b"Hello world", fd, 0)
                f = loop.create_future()
                op.set_callback(f.set_result)

                futures.append(f)
                ops.append(op)

            ctx.submit(*ops)
            await asyncio.gather(*futures)

            # op = Operation.fdsync(fd)
            # assert op.submit(ctx, efd1) == 1
            #
            # assert (await results.get())[0] == efd1.fileno
            #
            # assert fp.read() == b"Hello world"
            # fp.seek(0)
            #
            # op1 = AIOOperation.write(b"Hello from ", fd, 0)
            # op2 = AIOOperation.write(b"async world", fd, 11)
            # op1.prepare(efd1)
            # op2.prepare(efd2)
            #
            # assert ctx.submit(op1, op2) == 2
            #
            # res = [
            #     (await results.get())[0],
            #     (await results.get())[0],
            # ]
            #
            # assert sorted(res) == sorted([efd1.fileno, efd2.fileno])
            #
            # op = AIOOperation.fdsync(fd)
            # assert op.submit(ctx, efd1) == 1
            #
            # assert (await results.get())[0] == efd1.fileno
            #
            # assert fp.read() == b"Hello from async world"
            #
            # assert op1.eventfd == efd1
            # assert op2.eventfd == efd2
            #
            # assert op1.context == op2.context == ctx
            #
            # op = AIOOperation.read(255, fd, 0)
            # op.submit(ctx, efd1)
            #
            # res = await results.get()
            # assert res[0] == efd1.fileno
            #
            # ctx.process_events()
            #
            # assert op.get_value() == b"Hello from async world"

    for _ in range(10000):
        loop.run_until_complete(run())
        # loop.run_until_complete(asyncio.wait_for(run(), 1))
