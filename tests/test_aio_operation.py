import asyncio
import os

from caio._aio import Context, Operation


async def perform_operations(ctx: Context, *ops: Operation):
    futures = []
    loop = asyncio.get_event_loop()

    def on_read():
        ctx.poll()
        ctx.process_events()

    loop.add_reader(ctx.fileno, on_read)

    for op in ops:
        f = loop.create_future()
        op.set_callback(f.set_result)
        futures.append(f)

    ctx.submit(*ops)

    result = await asyncio.gather(*futures)

    loop.remove_reader(ctx.fileno)

    return result


def test_aio_operation(tmp_path):
    loop = asyncio.get_event_loop()
    ctx = Context(16)

    async def run():
        with open(os.path.join(tmp_path, "temp.bin"), "wb+") as fp:
            fd = fp.fileno()
            ops = []

            for _ in range(32):
                ops.append(Operation.write(b"Hello world", fd, 0))

            result = await perform_operations(ctx, *ops)
            assert result == [11] * 32

            op = Operation.fdsync(fd)
            assert await perform_operations(ctx, op) == [0]

            assert fp.read() == b"Hello world"
            fp.seek(0)

            op1 = Operation.write(b"Hello from ", fd, 0)
            op2 = Operation.write(b"async world", fd, 11)

            assert await perform_operations(ctx, op1, op2) == [11, 11]

            assert op1.context == ctx and op2.context == ctx

            op = Operation.fdsync(fd)
            assert await perform_operations(ctx, op) == [0]

            assert fp.read() == b"Hello from async world"

            op = Operation.read(255, fd, 0)
            await perform_operations(ctx, op)

            assert op.get_value() == b"Hello from async world"

    loop.run_until_complete(asyncio.wait_for(run(), 5))
