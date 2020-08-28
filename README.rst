Python wrapper for AIO
======================

.. warning:: ``fsync``/``fdsync`` operations in Linux aio implementation supports since 4.18. related calls will have no effect.

Python bindings for Linux AIO API and simple asyncio wrapper.

Example
-------

.. code-block:: python

    import asyncio
    from caio import AsyncioContext

    loop = asyncio.get_event_loop()

    async def main():
        # max_requests=128 by default
        ctx = AsyncioContext(max_requests=128)

        with open("test.file", "wb+") as fp:
            fd = fp.fileno()

            # Execute one write operation
            await ctx.write(b"Hello world", fd, offset=0)

            # Execute one read operation
            print(await ctx.read(32, fd, offset=0))

            # Execute one fdsync operation
            await ctx.fdsync(fd)

            op1 = ctx.write(b"Hello from ", fd, offset=0)
            op2 = ctx.write(b"async world", fd, offset=11)

            await asyncio.gather(op1, op2)

            print(await ctx.read(32, fd, offset=0))
            # Hello from async world


    loop.run_until_complete(main())
