from caio.version import *
import platform


if "linux" in platform.system().lower():
    from .linux_aio import Context, Operation
    from .linux_aio_asyncio import AsyncioContext
else:
    try:
        from .thread_aio import Context, Operation
        from .thread_aio_asyncio import AsyncioContext
    except Exception:
        from .python_aio import Context, Operation
        from .python_aio_asyncio import AsyncioContext


__all__ = (
    "Context",
    "Operation",
    "AsyncioContext",
    "__version__",
    "__author__",
)
