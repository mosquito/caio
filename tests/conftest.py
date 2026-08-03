import time

import pytest

from caio import variants, variants_asyncio


def drain(ctx, want, timeout=5.0):
    """Waits for `want` completions via whatever polling API the backend
    exposes, without busy-spinning (a tight CPU-bound retry loop can starve
    a backend's own background thread/kernel thread of scheduling time)
    and without silently giving up (raises on timeout instead) - both
    mistakes were made and fixed once already in ad hoc test scripts
    during caio's Rust rewrite; don't repeat them here."""
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
