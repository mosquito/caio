from . import python_aio, python_aio_asyncio
from .abstract import AbstractContext, AbstractOperation
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


variants = tuple(filter(None, [linux_aio, thread_aio, python_aio]))
variants_asyncio = tuple(filter(None, [
    linux_aio_asyncio,
    thread_aio_asyncio,
    python_aio_asyncio
]))

preferred = variants[0]
preferred_asyncio = variants_asyncio[0]


Context = preferred.Context      # type: ignore
Operation = preferred.Operation  # type: ignore
AsyncioContext = preferred_asyncio.AsyncioContext   # type: ignore


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
    "variants",
    "variants_asyncio",
)
