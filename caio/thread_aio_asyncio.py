from .thread_aio import Context, Operation
from .asyncio_base import AsyncioContextBase


class AsyncioContext(AsyncioContextBase):
    OPERATION_CLASS = Operation
    CONTEXT_CLASS = Context
