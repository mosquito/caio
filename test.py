from linux_aio.context import AIOContext
from linux_aio.eventfd import EventFD
from linux_aio.operation import AIOOperation


ctx = AIOContext(16)
print(ctx)

efd = EventFD()

op = AIOOperation.read(16, ctx, efd, 1, 0)

print(op)
print(op.fileno)
print(op.payload)
print(op.get_value())

op = AIOOperation.write(b"Hello world", ctx, efd, 1)

print(op)
print(op.fileno)
print(op.payload)
print(op.get_value())
