import functools
import threading
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


class ConcurrentThreads:
    """Run test workers and surface their failures in the main thread."""

    def __init__(self, timeout=30):
        self.timeout = timeout
        self._barriers = []
        self._threads = []
        self._errors = []
        self._errors_lock = threading.Lock()

    def barrier(self, parties):
        barrier = threading.Barrier(parties, timeout=self.timeout)
        self._barriers.append(barrier)
        return barrier

    def start(self, target, *args):
        thread = threading.Thread(
            target=self._run,
            args=(target, args),
            name=f"{target.__name__}-{len(self._threads)}",
        )
        self._threads.append(thread)
        thread.start()
        return thread

    def start_many(self, target, arguments):
        for args in arguments:
            self.start(target, *args)

    def join(self, timeout=None):
        deadline = time.monotonic() + (
            self.timeout if timeout is None else timeout
        )
        for thread in self._threads:
            thread.join(max(0, deadline - time.monotonic()))

        alive = [thread.name for thread in self._threads if thread.is_alive()]
        assert not alive, f"worker threads did not stop: {', '.join(alive)}"
        if self._errors:
            raise self._errors[0]

    def close(self):
        for barrier in self._barriers:
            barrier.abort()
        for thread in self._threads:
            thread.join(timeout=5)

    def _run(self, target, args):
        try:
            target(*args)
        except BaseException as exc:  # noqa: BLE001 (surface child-thread failures)
            with self._errors_lock:
                self._errors.append(exc)
            for barrier in self._barriers:
                barrier.abort()


@pytest.fixture
def workers():
    threads = ConcurrentThreads()
    yield threads
    threads.close()


@pytest.fixture
def submit_and_wait():
    def submit(context, operations, result_for=None, timeout=10):
        operations = tuple(operations)
        count = len(operations)
        results = [None] * count
        remaining = [count]
        errors = []
        lock = threading.Lock()
        done = threading.Event()

        if result_for is None:
            result_for = lambda _operation, result: result

        def make_callback(index, operation):
            def callback(result):
                with lock:
                    try:
                        results[index] = result_for(operation, result)
                    except BaseException as exc:  # noqa: BLE001 (surface callback failures)
                        errors.append(exc)
                    finally:
                        remaining[0] -= 1
                        if remaining[0] == 0:
                            done.set()

            return callback

        for index, operation in enumerate(operations):
            operation.set_callback(make_callback(index, operation))
            assert context.submit(operation) == 1

        assert done.wait(timeout), (
            f"only {count - remaining[0]}/{count} operations completed"
        )
        if errors:
            raise errors[0]
        return results

    return submit


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
