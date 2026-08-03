import time

import pytest

from caio import python_aio, thread_aio, variants, variants_asyncio

# thread_aio and python_aio only - the two backends actually backed by a
# bounded worker-thread pool (multiprocessing.pool.ThreadPool for both),
# where max_requests (acceptance capacity) and pool_size (worker count) are
# genuinely independent, so submitting more than pool_size at once leaves
# some operations sitting in the pool's own internal queue rather than
# already dispatched. linux_aio/linux_uring have no such queue - max_requests
# there bounds the kernel-visible I/O context directly, and neither Context
# accepts a pool_size argument at all. thread_aio may be None (unavailable
# on this platform/build) - filtering against variants (already None-free)
# handles that the same way as everywhere else.
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


def has_polling_api(backend):
    return hasattr(backend.Context, "process_events")


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


@pytest.fixture(params=variants)
def context_maker(request):
    return request.param.Context


@pytest.fixture(params=variants)
def operation_maker(request):
    return request.param.Operation


@pytest.fixture(params=variants)
def backend(request):
    # Unlike using context_maker/operation_maker together (which would
    # produce the cross product of both fixtures' independent
    # parametrization, mismatching e.g. thread_aio's Context with
    # linux_aio's Operation), this keeps Context/Operation from the same
    # backend module paired together.
    return request.param


@pytest.fixture(params=variants_asyncio)
def async_context_maker(request):
    return request.param.AsyncioContext


@pytest.fixture
def polling_backend(backend):
    if not has_polling_api(backend):
        pytest.skip(f"{backend.__name__} has no process_events()/poll() API")
    return backend
