import pytest

from caio import variants, variants_asyncio


@pytest.fixture(params=variants)
def context_maker(request):
    return request.param.Context


@pytest.fixture(params=variants_asyncio)
def async_context_maker(request):
    return request.param.AsyncioContext
