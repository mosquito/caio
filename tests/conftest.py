import pytest

from caio import python_aio, python_aio_asyncio


try:
    from caio import thread_aio, thread_aio_asyncio
except ImportError:
    thread_aio = None
    thread_aio_asyncio = None


try:
    from caio import linux_aio, linux_aio_asyncio
except ImportError:
    linux_aio = None
    linux_aio_asyncio = None


IMPLEMENTATIONS = list(filter(None, [linux_aio, python_aio, thread_aio]))


@pytest.fixture(params=IMPLEMENTATIONS)
def context_maker(request):
    return request.param.Context


IMPLEMENTATIONS_ASYNC = list(
    filter(
        None, [
            linux_aio_asyncio,
            thread_aio_asyncio,
            python_aio_asyncio,
        ],
    ),
)


@pytest.fixture(params=IMPLEMENTATIONS_ASYNC)
def async_context_maker(request):
    return request.param.AsyncioContext
