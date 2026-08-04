import functools
import time
import types

import pytest

from caio import (
    linux_aio,
    linux_aio_asyncio,
    linux_uring,
    linux_uring_asyncio,
    python_aio,
    thread_aio,
    variants,
    variants_asyncio,
)


def named_variant(name, **attrs):
    ns = types.SimpleNamespace(__name__=name, **attrs)
    return ns


# Test-only sqpoll/deferred variants - not part of caio's public API.
extra_context_variants = []
extra_asyncio_variants = []

if linux_uring is not None:
    extra_context_variants.append(named_variant(
        "linux_uring[sqpoll=True]",
        Context=functools.partial(linux_uring.Context, sqpoll=True),
        Operation=linux_uring.Operation,
    ))
    extra_asyncio_variants.append(named_variant(
        "linux_uring[sqpoll=True]",
        AsyncioContext=functools.partial(linux_uring_asyncio.AsyncioContext, sqpoll=True),
    ))
    # sqpoll=False forced: SQPOLL_ALLOWED's default would otherwise mask
    # the deferred-batching path this variant exists to cover.
    extra_asyncio_variants.append(named_variant(
        "linux_uring[deferred=True]",
        AsyncioContext=functools.partial(
            linux_uring_asyncio.AsyncioContext, sqpoll=False, deferred=True,
        ),
    ))

if linux_aio is not None:
    extra_asyncio_variants.append(named_variant(
        "linux_aio[deferred=True]",
        AsyncioContext=functools.partial(linux_aio_asyncio.AsyncioContext, deferred=True),
    ))

all_variants = variants + tuple(extra_context_variants)
all_variants_asyncio = variants_asyncio + tuple(extra_asyncio_variants)

# extra_context_variants are always linux_uring, always polling-capable -
# added directly rather than via hasattr() (doesn't see through partial).
polling_variants = [
    v for v in variants if hasattr(v.Context, "process_events")
] + extra_context_variants

# Only thread_aio/python_aio have a worker-pool queue distinct from
# "accepted" - max_requests/pool_size are independent there.
pooled_variants = tuple(v for v in variants if v in (thread_aio, python_aio))


@pytest.fixture(params=pooled_variants)
def pooled_backend(request):
    return request.param


def drain(ctx, want, timeout=5.0):
    """Waits for `want` completions via whatever polling API the backend
    exposes, without busy-spinning (a tight CPU-bound retry loop can starve
    a backend's own background thread/kernel thread of scheduling time)
    and without silently giving up (raises on timeout instead)."""
    total = 0
    if hasattr(ctx, "flush"):
        total += ctx.flush()
    deadline = time.monotonic() + timeout
    while total < want:
        if time.monotonic() > deadline:
            raise TimeoutError(f"drain: only {total}/{want} completions after {timeout}s")
        time.sleep(0.001)
        total += ctx.process_events()
    return total


def wait_until(ctx, predicate, timeout=5.0):
    """Like drain(), but works across every backend uniformly, including
    the two purely callback-driven ones (thread_aio/python_aio, whose
    Context has no flush()/process_events() at all - results just arrive
    on a background worker thread on their own). Polls an arbitrary
    predicate (e.g. `lambda: len(results) >= n`) instead of a completion
    count, pumping the context each iteration only for backends that
    actually need pumping to make progress."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError(f"condition not met within {timeout}s")
        if hasattr(ctx, "flush"):
            ctx.flush()
        if hasattr(ctx, "process_events"):
            ctx.process_events()
        time.sleep(0.001)
    return True


@pytest.fixture(params=all_variants)
def context_maker(request):
    return request.param.Context


@pytest.fixture(params=all_variants)
def operation_maker(request):
    return request.param.Operation


@pytest.fixture(params=all_variants)
def backend(request):
    # Keeps Context/Operation paired from the same backend, unlike
    # context_maker+operation_maker's independent cross product.
    return request.param


@pytest.fixture(params=all_variants_asyncio)
def async_context_maker(request):
    return request.param.AsyncioContext


@pytest.fixture(params=all_variants_asyncio)
async def async_context(request):
    """Ready, already-entered AsyncioContext, default args. Use
    async_context_maker instead for a non-default constructor kwarg."""
    async with request.param.AsyncioContext() as context:
        yield context


@pytest.fixture(params=polling_variants)
def polling_backend(request):
    return request.param
