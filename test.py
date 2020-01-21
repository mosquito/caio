import os

from linux_aio import AIOContext, EventFD, AIOOperation

from tempfile import NamedTemporaryFile


ctx = AIOContext()
print(ctx)

efd = EventFD()
print(efd)

fpw = NamedTemporaryFile(mode="bw")

fd = os.open(fpw.name, os.O_RDWR | os.O_CREAT)

print(ctx.submit(
    AIOOperation.read(16, fd, 0),
    AIOOperation.write(b"Hello world", fd, 0)
))

# op = AIOOperation.read(16, fd, 0)
# print(op, ctx, efd, op.fileno, op.offset, op.priority)
# print(op.submit(ctx, efd))

# op = AIOOperation.write(b"Hello world", fd, 0)
# print(op, ctx, efd, op.fileno, op.offset, op.priority)
# print(op.submit(ctx, efd))

# op = AIOOperation.write(b"Hello world", 1, 0)
#
# print(op)
# print(op.fileno)
# print(op.payload)
# print(op.get_value())
