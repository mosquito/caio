import platform
from .asyncio_adapter import AsyncioContext


if 'linux' in platform.system().lower():
    from .linux_aio import Context, Operation
else:
    from .thread_aio import Context, Operation
