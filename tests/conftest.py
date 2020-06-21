import pytest

from caio import variants, variants_asyncio, Context, AsyncioContext


@pytest.fixture(params=variants + (Context,))
def context_maker(request):
    return request.param.Context


@pytest.fixture(params=variants_asyncio + (AsyncioContext,))
def async_context_maker(request):
    return request.param.AsyncioContext
