Linux AIO for python
====================

Python bindings for Linux AIO API.

Example
-------

.. code-block:: python

    from linux_aio import AIOContext, EventFD, AIOOperation


    loop = asyncio.get_event_loop()

    def on_read(fileno, future):
        future.set_result(os.read(fileno, 8))
        loop.remove_reader(fileno)


    async def wait_aio_operation(op, ctx, efd):
        result = loop.create_future()
        loop.add_reader(efd.fileno, on_read, efd.fileno, result)

        op.submit(ctx, efd)

        return await result


    async def main():
        ctx = AIOContext()
        efd  = EventFD()

        with open("test.file"), "wb+") as fp:
            fd = fp.fileno()

            # Execute one write operation
            await wait_aio_operation(
                AIOOperation.write(b"Hello world", fd, offset=0)
            )

            # Execute one read operation
            op = AIOOperation.read(32, fd, offset=0)
            await wait_aio_operation(op)

            print(op.get_result())

            # Execute one read operation
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
