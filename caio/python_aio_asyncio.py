from .python_aio import Context, Operation
from .asyncio_base import AsyncioContextBase


class AsyncioContext(AsyncioContextBase):
    OPERATION_CLASS = Operation
    CONTEXT_CLASS = Context

    def _destroy_context(self):
        self.context.close()
