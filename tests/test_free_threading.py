import gc
import os
import select
import sys
import sysconfig
import threading
import time
import weakref

import pytest

import caio
from caio import python_aio

# os.open() defaults to text mode on Windows without this - \n <-> \r\n
# translation would corrupt any payload containing a raw \n byte.
_O_BINARY = getattr(os, "O_BINARY", 0)


def test_importing_caio_does_not_enable_gil():
    """Every importable C backend must declare and support free threading."""
    if not sysconfig.get_config_var("Py_GIL_DISABLED"):
        pytest.skip("not a free-threaded build")
    assert sys._is_gil_enabled() is False, (
        "importing caio enabled the GIL; at least one C extension has not "
        "declared free-threading support"
    )


def test_high_concurrency_stress_no_data_races(tmp_path, submit_and_wait):
    """Hammers python_aio's own bookkeeping (the _lock-protected
    _in_progress counter and per-operation in_progress flag) with many
    concurrent writes then reads dispatched across a real multi-worker
    ThreadPool. Under a free-threaded interpreter these workers can run
    truly in parallel instead of being serialized by the GIL, so a race in
    that bookkeeping - two workers claiming the same slot, a result
    crossing over to the wrong Operation - would show up here as a lost
    write or a chunk read back with the wrong content, not just a
    thread-timing fluke."""
    count = 200
    chunk = 4096
    path = tmp_path / "stress.bin"
    path.write_bytes(b"\x00" * (count * chunk))
    fd = os.open(str(path), os.O_RDWR | _O_BINARY)
    ctx = None
    try:
        ctx = python_aio.Context(max_requests=count, pool_size=32)
        expected = [bytes([i % 256]) * chunk for i in range(count)]

        writes = (
            python_aio.Operation.write(payload, fd, index * chunk)
            for index, payload in enumerate(expected)
        )
        written = submit_and_wait(ctx, writes)
        assert written == [chunk] * count

        reads = (
            python_aio.Operation.read(chunk, fd, index * chunk)
            for index in range(count)
        )
        read_back = submit_and_wait(
            ctx,
            reads,
            result_for=lambda operation, _result: operation.get_value(),
        )
        assert read_back == expected
    finally:
        os.close(fd)
        if ctx is not None:
            ctx.close()


def test_same_operation_is_claimed_once_across_contexts(workers):
    """An Operation's one-shot claim must be synchronized by the Operation.

    Context._execute() currently protects ``operation.in_progress`` with the
    Context's lock.  That only serializes submissions through the *same*
    Context: two Contexts use two unrelated locks and can both observe False
    before either stores True when the GIL is disabled.

    Synchronizing each pair of submissions makes the race frequent enough to
    be a useful regression test without depending on filesystem timing.
    """
    if getattr(sys, "_is_gil_enabled", lambda: True)():
        pytest.skip("requires a free-threaded interpreter with the GIL disabled")

    iterations = 20_000
    contexts = (
        python_aio.Context(max_requests=iterations * 2),
        python_aio.Context(max_requests=iterations * 2),
    )
    start = workers.barrier(3)
    finished = workers.barrier(3)
    current = [None]
    submitted = [0, 0]

    def submitter(index, context):
        for _ in range(iterations):
            start.wait()
            submitted[index] += context.submit(current[0])
            finished.wait()

    try:
        workers.start_many(submitter, enumerate(contexts))

        for _ in range(iterations):
            current[0] = python_aio.Operation(
                0, None, None, python_aio.OpCode.NOOP,
            )
            start.wait()
            finished.wait()

        workers.join()
        assert sum(submitted) == iterations, (
            f"{sum(submitted) - iterations} Operations were submitted twice"
        )
    finally:
        workers.close()
        for context in contexts:
            context.close()
            context.pool.join()


def _require_gil_disabled():
    if getattr(sys, "_is_gil_enabled", lambda: True)():
        pytest.skip("requires a free-threaded interpreter with the GIL disabled")


def _pump_contexts(contexts, predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("contexts did not complete in time")
        for context in contexts:
            if hasattr(context, "flush"):
                context.flush()
        for context in contexts:
            if hasattr(context, "process_events"):
                context.process_events()
        time.sleep(0.001)


def _close_contexts(contexts):
    for context in contexts:
        close = getattr(context, "close", None)
        if close is not None:
            close()
    for context in contexts:
        pool = getattr(context, "pool", None)
        if pool is not None:
            pool.join()


@pytest.fixture(
    params=[2, 4, 8, 16, 32],
    ids=lambda value: f"submitters={value}",
)
def ft_submitter_count(request):
    return request.param


@pytest.fixture(params=[1, 3, 17, 64], ids=lambda value: f"batch={value}")
def ft_operations_per_submit(request):
    return request.param


@pytest.fixture(params=[1, 8], ids=lambda value: f"submit-rounds={value}")
def ft_submit_rounds(request):
    return request.param


@pytest.fixture(params=[2, 4, 8], ids=lambda value: f"claimants={value}")
def ft_claimant_count(request):
    return request.param


@pytest.fixture(
    params=[257, 4_096, 20_000],
    ids=lambda value: f"claims={value}",
)
def ft_claim_operation_count(request):
    return request.param


@pytest.fixture(
    params=[63, 256, 4_096],
    ids=lambda value: f"operations={value}",
)
def ft_operation_count(request):
    return request.param


@pytest.fixture(
    params=[2, 4, 8, 16],
    ids=lambda value: f"drainers={value}",
)
def ft_drainer_count(request):
    return request.param


@pytest.fixture(params=[1, 3, 8, 32], ids=lambda value: f"rounds={value}")
def ft_drain_rounds(request):
    return request.param


@pytest.fixture(params=[2, 8, 16], ids=lambda value: f"readers={value}")
def ft_reader_count(request):
    return request.param


@pytest.fixture(params=[1, 257, 2_000], ids=lambda value: f"reads={value}")
def ft_reads_per_thread(request):
    return request.param


@pytest.fixture(params=[2, 8, 16], ids=lambda value: f"observers={value}")
def ft_observer_count(request):
    return request.param


@pytest.fixture(
    params=[257, 2_000],
    ids=lambda value: f"completions={value}",
)
def ft_completion_count(request):
    return request.param


@pytest.fixture(params=[2, 8, 32], ids=lambda value: f"setters={value}")
def ft_callback_setter_count(request):
    return request.param


@pytest.fixture(params=[1, 257, 2_000], ids=lambda value: f"sets={value}")
def ft_callback_sets_per_thread(request):
    return request.param


@pytest.fixture(params=[2, 8, 32], ids=lambda value: f"cancellers={value}")
def ft_canceller_count(request):
    return request.param


@pytest.fixture(params=[1, 8, 64], ids=lambda value: f"cancel-rounds={value}")
def ft_cancel_rounds(request):
    return request.param


def test_same_operation_is_claimed_once_across_contexts_all_backends(
    tmp_path,
    backend,
    ft_claimant_count,
    ft_claim_operation_count,
    workers,
):
    """Every backend must synchronize a one-shot claim on the Operation.

    This is deliberately separate from the more aggressive python_aio test
    above: it only uses the shared public API, so future free-threading C
    backends run exactly the same cross-Context race.
    """
    _require_gil_disabled()

    iterations = ft_claim_operation_count
    claim_window = min(iterations, 512)
    path = tmp_path / "cross-context-claim.bin"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | _O_BINARY, 0o600)
    contexts = tuple(
        backend.Context(max_requests=claim_window)
        for _ in range(ft_claimant_count)
    )
    start = workers.barrier(ft_claimant_count + 1)
    finished = workers.barrier(ft_claimant_count + 1)
    current = [None]
    submitted = [0] * ft_claimant_count
    callback_count = [0]
    callback_lock = threading.Lock()

    def on_done(_result):
        with callback_lock:
            callback_count[0] += 1

    def submitter(index, context):
        for _ in range(iterations):
            start.wait()
            submitted[index] += context.submit(current[0])
            finished.wait()

    try:
        workers.start_many(submitter, enumerate(contexts))

        for iteration in range(iterations):
            operation = backend.Operation.write(b"x", fd, 0)
            operation.set_callback(on_done)
            current[0] = operation
            start.wait()
            finished.wait()
            if (iteration + 1) % claim_window == 0:
                expected_callbacks = sum(submitted)

                def window_finished(expected=expected_callbacks):
                    with callback_lock:
                        return callback_count[0] >= expected

                _pump_contexts(
                    contexts,
                    window_finished,
                    timeout=20,
                )

        workers.join()
        accepted = sum(submitted)

        def all_callbacks_finished():
            with callback_lock:
                return callback_count[0] >= accepted

        _pump_contexts(
            contexts,
            all_callbacks_finished,
            timeout=20,
        )
        assert accepted == iterations, (
            f"{accepted - iterations} Operations were submitted twice"
        )
        assert callback_count[0] == iterations
    finally:
        workers.close()
        _close_contexts(contexts)
        os.close(fd)


def test_concurrent_submits_to_one_context_all_backends(
    tmp_path,
    backend,
    ft_submitter_count,
    ft_operations_per_submit,
    ft_submit_rounds,
    workers,
):
    """Distinct Operations submitted concurrently must not corrupt Context."""
    _require_gil_disabled()

    worker_count = ft_submitter_count
    operations_per_worker = ft_operations_per_submit
    submit_rounds = ft_submit_rounds
    operations_per_worker_total = operations_per_worker * submit_rounds
    total = worker_count * operations_per_worker_total
    path = tmp_path / "one-context-submit.bin"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | _O_BINARY, 0o600)
    context = backend.Context(max_requests=total)
    submit_start = workers.barrier(worker_count)
    operations = []
    callback_results = [None] * total
    callback_lock = threading.Lock()
    submitted = [0] * worker_count

    def make_callback(index, operation):
        def callback(result):
            with callback_lock:
                callback_results[index] = (result, operation.get_value())
        return callback

    for index in range(total):
        payload = index.to_bytes(4, "little")
        operation = backend.Operation.write(payload, fd, index * 4)
        operation.set_callback(make_callback(index, operation))
        operations.append(operation)

    def submitter(worker_index):
        first = worker_index * operations_per_worker_total
        accepted = 0
        for round_index in range(submit_rounds):
            batch_first = first + round_index * operations_per_worker
            batch_last = batch_first + operations_per_worker
            # Every round stages a batch after the same barrier. Multiple
            # rounds vary scheduling and repeatedly collide inside the
            # Context's SQ-tail read/write window.
            submit_start.wait()
            accepted += context.submit(
                *operations[batch_first:batch_last],
            )
        submitted[worker_index] = accepted

    try:
        workers.start_many(
            submitter,
            ((index,) for index in range(worker_count)),
        )
        workers.join()

        assert sum(submitted) == total

        def all_callbacks_finished():
            with callback_lock:
                return all(
                    result is not None
                    for result in callback_results
                )

        _pump_contexts(
            (context,),
            all_callbacks_finished,
            timeout=20,
        )
        assert callback_results == [(4, 4)] * total

        os.lseek(fd, 0, os.SEEK_SET)
        expected = b"".join(index.to_bytes(4, "little") for index in range(total))
        assert os.read(fd, len(expected)) == expected
    finally:
        workers.close()
        _close_contexts((context,))
        os.close(fd)


def test_concurrent_process_events_delivers_each_completion_once(
    tmp_path,
    polling_backend,
    ft_operation_count,
    ft_drainer_count,
    ft_drain_rounds,
    workers,
):
    """Concurrent drainers must never consume or callback one event twice."""
    _require_gil_disabled()

    operation_count = ft_operation_count
    drainer_count = ft_drainer_count
    drain_rounds = ft_drain_rounds
    path = tmp_path / "concurrent-process-events.bin"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | _O_BINARY, 0o600)
    context = polling_backend.Context(max_requests=operation_count)
    callback_counts = [0] * operation_count
    callback_lock = threading.Lock()
    drain_round = workers.barrier(drainer_count)

    def make_callback(index):
        def callback(_result):
            with callback_lock:
                callback_counts[index] += 1
        return callback

    operations = []
    for index in range(operation_count):
        operation = polling_backend.Operation.write(b"x", fd, index)
        operation.set_callback(make_callback(index))
        operations.append(operation)

    assert context.submit(*operations) == operation_count
    is_sqpoll = bool(getattr(context, "sqpoll", False))
    if hasattr(context, "flush"):
        context.flush()
    if is_sqpoll:
        with callback_lock:
            completed_inline = sum(callback_counts)
        if completed_inline < operation_count:
            # flush() above wakes a sleeping SQPOLL thread and may drain
            # already-ready CQEs. If work remains, wait until eventfd says
            # the kernel has populated more of the CQ, but leave that CQ
            # untouched for the synchronized drainers below.
            readable, _, _ = select.select([context.fileno], [], [], 10)
            assert readable, "SQPOLL produced no completion notification"
            time.sleep(0.01)

    def drain():
        for _ in range(drain_rounds):
            # Force all drainers to enter every round together. Without
            # this barrier a fast first thread can consume the whole CQ
            # before the others even start, accidentally serializing the
            # test and hiding a duplicate cq_head read/commit.
            drain_round.wait()
            context.process_events(
                max_requests=operation_count,
                min_requests=0,
                timeout=0,
            )

    try:
        workers.start_many(drain, (() for _ in range(drainer_count)))
        workers.join()

        def all_callbacks_finished():
            with callback_lock:
                return sum(callback_counts) >= operation_count

        # Fixed synchronized rounds should normally drain everything. Finish
        # any genuinely late kernel completions serially so the assertion
        # below diagnoses duplicate delivery, not storage latency.
        _pump_contexts((context,), all_callbacks_finished, timeout=20)
        assert callback_counts == [1] * operation_count
    finally:
        workers.close()
        os.close(fd)


def test_completed_operation_supports_concurrent_readers(
    tmp_path,
    backend,
    ft_reader_count,
    ft_reads_per_thread,
    workers,
):
    """C backends must safely return owned result references to all threads."""
    _require_gil_disabled()

    payload = bytes(range(256)) * 16
    path = tmp_path / "concurrent-result-readers.bin"
    path.write_bytes(payload)
    fd = os.open(path, os.O_RDONLY | _O_BINARY)
    context = backend.Context(max_requests=8)
    operation = backend.Operation.read(len(payload), fd, 0)
    completed = threading.Event()
    operation.set_callback(lambda _result: completed.set())
    assert context.submit(operation) == 1
    _pump_contexts((context,), completed.is_set)
    assert operation.get_value() == payload

    reader_count = ft_reader_count
    reads_per_thread = ft_reads_per_thread
    start = workers.barrier(reader_count + 1)

    def read_result():
        start.wait()
        for _ in range(reads_per_thread):
            if operation.get_value() != payload:
                raise AssertionError("concurrent get_value() returned wrong data")

    try:
        workers.start_many(read_result, (() for _ in range(reader_count)))
        start.wait()
        workers.join()
    finally:
        workers.close()
        _close_contexts((context,))
        os.close(fd)


def test_payload_access_is_safe_while_worker_publishes_completion(
    tmp_path,
    pooled_backend,
    ft_observer_count,
    ft_completion_count,
    workers,
):
    """Publishing ``done`` must not expose a buffer being cleared concurrently.

    In thread_aio the worker used to release-store ``done = 1`` before
    acquiring a Python thread state and clearing a completed write's
    ``py_buffer``. A free-running reader could therefore pass the in-flight
    check and Py_INCREF the same pointer while the worker Py_DECREFed it.
    """
    _require_gil_disabled()

    operation_count = ft_completion_count
    observer_count = ft_observer_count
    path = tmp_path / "payload-completion-race.bin"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | _O_BINARY, 0o600)
    context = pooled_backend.Context(
        max_requests=operation_count + 1,
        pool_size=1,
    )

    blocker_started = threading.Event()
    blocker_release = threading.Event()
    blocker = pooled_backend.Operation.fsync(fd)
    blocker.set_callback(
        lambda _result: (
            blocker_started.set(),
            blocker_release.wait(30),
        ),
    )
    assert context.submit(blocker) == 1
    assert blocker_started.wait(10), "worker did not enter blocker callback"

    completed_count = [0]
    completed_lock = threading.Lock()
    all_completed = threading.Event()
    operations = []

    def on_done(_result):
        # thread_aio publishes done before invoking this callback and clears
        # the write buffer immediately after it returns. Yielding here makes
        # observers repeatedly enter payload/get_value in that exact state
        # and then overlap the buffer's ownership transition.
        time.sleep(0.001)
        with completed_lock:
            completed_count[0] += 1
            if completed_count[0] == operation_count:
                all_completed.set()

    for index in range(operation_count):
        operation = pooled_backend.Operation.write(b"x", fd, index)
        operation.set_callback(on_done)
        operations.append(operation)

    assert context.submit(*operations) == operation_count

    observer_start = workers.barrier(observer_count + 1)

    def observe():
        observer_start.wait()
        while not all_completed.is_set():
            for operation in operations:
                try:
                    payload = operation.payload
                    if payload is not None:
                        bytes(payload)
                except RuntimeError:
                    pass

                try:
                    operation.get_value()
                except RuntimeError:
                    pass

    try:
        workers.start_many(observe, (() for _ in range(observer_count)))
        observer_start.wait()
        blocker_release.set()
        assert all_completed.wait(30), "worker did not finish queued operations"
        workers.join(timeout=10)

        assert completed_count[0] == operation_count
    finally:
        blocker_release.set()
        workers.close()
        _close_contexts((context,))
        os.close(fd)


def test_concurrent_callback_replacement_releases_every_old_callback(
    backend,
    ft_callback_setter_count,
    ft_callback_sets_per_thread,
    workers,
):
    """set_callback must atomically replace and release its owned reference."""
    _require_gil_disabled()

    setter_count = ft_callback_setter_count
    sets_per_thread = ft_callback_sets_per_thread
    operation = backend.Operation.fsync(0)
    start = workers.barrier(setter_count + 1)

    class Callback:
        def __call__(self, _result):
            return None

    callbacks = [
        Callback()
        for _ in range(setter_count * sets_per_thread)
    ]
    callback_refs = [weakref.ref(callback) for callback in callbacks]

    def replace_callbacks(thread_index):
        start.wait()
        first = thread_index * sets_per_thread
        for index in range(first, first + sets_per_thread):
            assert operation.set_callback(callbacks[index]) is True

    try:
        workers.start_many(
            replace_callbacks,
            ((index,) for index in range(setter_count)),
        )
        start.wait()
        workers.join()

        # Move the Operation off every callback in the contested set. Each
        # set_callback owns exactly one reference and must release the old
        # one, even when several threads replace the same slot concurrently.
        operation.set_callback(Callback())
        callbacks.clear()
        gc.collect()
        leaked = sum(ref() is not None for ref in callback_refs)
        assert leaked == 0, f"{leaked} replaced callbacks are still referenced"
    finally:
        workers.close()


def test_concurrent_uring_cancel_requests_do_not_corrupt_submission_ring(
    ft_canceller_count,
    ft_cancel_rounds,
    workers,
):
    """cancel and submit share io_uring's SQ tail and must share its lock."""
    _require_gil_disabled()
    if caio.linux_uring is None:
        pytest.skip("linux_uring backend is unavailable")

    canceller_count = ft_canceller_count
    cancel_rounds = ft_cancel_rounds
    operation_count = canceller_count * cancel_rounds
    context = caio.linux_uring.Context(
        max_requests=operation_count * 2,
        sqpoll=False,
    )
    read_fd, write_fd = os.pipe()
    callback_counts = [0] * operation_count
    callback_lock = threading.Lock()

    def make_callback(index):
        def callback(_result):
            with callback_lock:
                callback_counts[index] += 1
        return callback

    targets = [
        caio.linux_uring.Operation.read(
            1,
            read_fd,
            (1 << 64) - 1,
        )
        for _ in range(operation_count)
    ]
    for index, operation in enumerate(targets):
        operation.set_callback(make_callback(index))
    assert context.submit(*targets) == operation_count
    context.flush()

    start_round = workers.barrier(canceller_count)
    cancelled = [0] * canceller_count

    def cancel(thread_index):
        for round_index in range(cancel_rounds):
            start_round.wait()
            cancelled[thread_index] += context.cancel(
                targets[round_index * canceller_count + thread_index],
            )

    try:
        workers.start_many(
            cancel,
            ((index,) for index in range(canceller_count)),
        )
        workers.join()

        assert cancelled == [0] * canceller_count

        def all_targets_completed():
            with callback_lock:
                return sum(callback_counts) >= operation_count

        _pump_contexts((context,), all_targets_completed, timeout=10)
        assert callback_counts == [1] * operation_count
    finally:
        workers.close()
        # Release any read whose cancel SQE was lost so Context teardown
        # cannot leave the kernel holding pointers to live Operations.
        try:
            os.write(write_fd, b"x" * operation_count)
            _pump_contexts(
                (context,),
                lambda: sum(callback_counts) >= operation_count,
                timeout=5,
            )
        except (BrokenPipeError, TimeoutError):
            pass
        os.close(write_fd)
        os.close(read_fd)
