from .abstract import AbstractContext, AbstractOperation

from . import python_aio
from . import python_aio_asyncio
from .version import __author__, __version__


try:
    from . import linux_aio
    from . import linux_aio_asyncio
except ImportError:
    linux_aio = None            # type: ignore
    linux_aio_asyncio = None    # type: ignore

try:
    from . import thread_aio
    from . import thread_aio_asyncio
except ImportError:
    thread_aio = None           # type: ignore
    thread_aio_asyncio = None   # type: ignore


preferred = list(filter(None, [linux_aio, thread_aio, python_aio]))[0]


Context = preferred.Context      # type: ignore
Operation = preferred.Operation  # type: ignore


__all__ = (
    "Context",
    "Operation",
    "AsyncioContext",
    "AbstractContext",
    "AbstractOperation",
    "python_aio",
    "python_aio_asyncio",
    "linux_aio",
    "linux_aio_asyncio",
    "thread_aio",
    "thread_aio_asyncio",
    "__version__",
    "__author__",
)
