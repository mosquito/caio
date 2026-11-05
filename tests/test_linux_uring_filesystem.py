"""Native io_uring file lifecycle and metadata operations.

These tests use a real ring. PausedContext stages SQEs without submitting
them, so a test can cancel a task while its operation is provably in flight
and then release the kernel work with an explicit flush().
"""

import asyncio
import errno
import gc
import os
import stat
import weakref
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from unittest.mock import patch

import aiomisc
import pytest
from conftest import import_backend_or_skip

from caio.abstract import CloseError

AT_REMOVEDIR = 0x200
AT_EMPTY_PATH = 0x1000
AT_FDCWD = -100


@pytest.fixture
def uring():
    return import_backend_or_skip("caio.linux_uring_asyncio")


@pytest.fixture
def paused_context(uring):
    class PausedContext(uring.AsyncioContext):
        """A real non-SQPOLL ring that admits SQEs only on flush()."""

        def __init__(self, *, max_requests=1):
            self.submissions = asyncio.Queue()
            super().__init__(max_requests=max_requests, sqpoll=False)

        def _submit_op(self, op, future):
            super()._submit_op(op, future)
            self.submissions.put_nowait(op)

        def _on_submitted(self):
            pass

        def flush(self):
            self.context.flush()

    return PausedContext


async def checkpoint():
    """Run ready callbacks once without depending on wall-clock timing."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    loop.call_soon(future.set_result, None)
    await future


def assert_closed(fd):
    with pytest.raises(OSError) as caught:
        os.fstat(fd)
    assert caught.value.errno == errno.EBADF


def wait_for_completion(context, operation):
    context.flush()
    if not operation.completed:
        context.process_events(min_requests=1, timeout=5)
    assert operation.completed


@aiomisc.timeout(5)
async def test_open_stat_truncate_close(tmp_path, uring):
    async with uring.AsyncioContext(sqpoll=False) as context:
        fd = await context.open(
            tmp_path / "file", os.O_CREAT | os.O_RDWR, 0o600,
        )
        try:
            assert not os.get_inheritable(fd)
            assert await context.write(b"abcdef", fd, 0) == 6
            assert await context.read(20, fd, 0) == b"abcdef"

            expected = os.fstat(fd)
            actual = await context.stat(fd)
            assert isinstance(actual, os.stat_result)
            for name in (
                "st_mode", "st_ino", "st_dev", "st_nlink", "st_uid",
                "st_gid", "st_size", "st_atime_ns", "st_mtime_ns",
                "st_ctime_ns", "st_rdev", "st_blksize", "st_blocks",
            ):
                assert getattr(actual, name) == getattr(expected, name), name
            assert tuple(actual) == tuple(expected)

            await context.truncate(fd, 3)
            assert (await context.stat(fd)).st_size == 3
            assert await context.read(20, fd, 0) == b"abc"
            await context.truncate(fd, 8)
            assert await context.read(20, fd, 0) == b"abc\0\0\0\0\0"
            await context.truncate(fd)
            assert (await context.stat(fd)).st_size == 0
        finally:
            await context.close_file(fd)
        assert_closed(fd)


@aiomisc.timeout(5)
async def test_paths_and_symlink_policy(tmp_path, uring):
    async with uring.AsyncioContext(sqpoll=False) as context:
        directory = tmp_path / "directory"
        assert await context.mkdir(directory, 0o700) is None

        # Undecodable bytes must reach the kernel unchanged.
        encoded = os.fsencode(directory / "file") + b"\xff"
        fd = await context.open(encoded, os.O_CREAT | os.O_RDWR, 0o600)
        await context.close_file(fd)

        renamed = directory / "renamed"
        hardlink = directory / "hardlink"
        symlink = directory / "symlink"
        assert await context.rename(encoded, renamed) is None
        assert await context.link(renamed, hardlink) is None
        assert await context.symlink(b"renamed", os.fsencode(symlink)) is None

        regular = await context.stat(renamed)
        assert (await context.stat(hardlink)).st_ino == regular.st_ino
        assert (await context.stat(symlink)).st_ino == regular.st_ino
        lstat = await context.stat(symlink, follow_symlinks=False)
        assert stat.S_ISLNK(lstat.st_mode)

        await context.unlink(renamed)
        with pytest.raises(FileNotFoundError):
            await context.stat(symlink)
        lstat = await context.stat(symlink, follow_symlinks=False)
        assert stat.S_ISLNK(lstat.st_mode)

        await context.unlink(hardlink)
        await context.unlink(symlink)
        assert await context.rmdir(directory) is None
        assert not directory.exists()


@aiomisc.timeout(5)
async def test_errno_and_filenames(tmp_path, uring):
    missing = tmp_path / "missing"
    destination = tmp_path / "destination"
    async with uring.AsyncioContext(sqpoll=False) as context:
        for operation in (
            context.open(missing, os.O_RDONLY),
            context.stat(missing),
            context.unlink(missing),
        ):
            with pytest.raises(FileNotFoundError) as caught:
                await operation
            assert caught.value.errno == errno.ENOENT
            assert caught.value.filename == str(missing)

        with pytest.raises(FileNotFoundError) as caught:
            await context.rename(missing, destination)
        assert caught.value.filename == str(missing)
        assert caught.value.filename2 == str(destination)

        with pytest.raises(FileExistsError):
            await context.mkdir(tmp_path)
        with pytest.raises(IsADirectoryError):
            await context.unlink(tmp_path)

        with pytest.raises(OSError) as caught:
            await context.close_file(-1)
        assert caught.value.errno == errno.EBADF

        fd = await context.open(tmp_path / "file", os.O_CREAT | os.O_RDWR)
        try:
            with pytest.raises(OSError) as caught:
                await context.truncate(fd, -1)
            assert caught.value.errno == errno.EINVAL
        finally:
            await context.close_file(fd)

        with pytest.raises(ValueError):
            await context.stat(0, follow_symlinks=False)


@pytest.mark.parametrize("fd", [-1, AT_FDCWD, -(1 << 31)])
@aiomisc.timeout(5)
async def test_stat_rejects_negative_fds(fd, uring):
    async with uring.AsyncioContext(sqpoll=False) as context:
        with pytest.raises(OSError) as caught:
            await context.stat(fd)
        assert caught.value.errno == errno.EBADF
        assert caught.value.filename is None


@aiomisc.timeout(5)
async def test_low_level_statx_accepts_at_fdcwd(uring):
    async with uring.AsyncioContext(sqpoll=False) as context:
        result = await context.submit(
            uring.Operation.statx(".", dir_fd=AT_FDCWD),
        )
    expected = Path.cwd().stat()
    assert result.st_ino == expected.st_ino
    assert result.st_dev == expected.st_dev
    assert stat.S_ISDIR(result.st_mode)


@aiomisc.timeout(5)
async def test_invalid_inputs_rejected_before_submission(tmp_path, uring):
    Operation = uring.Operation
    async with uring.AsyncioContext(sqpoll=False) as context:
        for path in ("file\0tail", b"file\0tail"):
            with pytest.raises(ValueError):
                await context.open(path, os.O_RDONLY)
            with pytest.raises(ValueError):
                await context.stat(path)
            with pytest.raises(ValueError):
                await context.rename(tmp_path / "source", path)

    with pytest.raises(OverflowError):
        Operation.statx(b"", dir_fd=1 << 40, flags=AT_EMPTY_PATH)
    with pytest.raises(OverflowError):
        Operation.ftruncate(0, 1 << 80)
    with pytest.raises(RuntimeError):
        Operation.openat(tmp_path / "not-created", os.O_CREAT).get_value()
    assert not (tmp_path / "not-created").exists()


@aiomisc.timeout(5)
async def test_no_userspace_or_executor_fallback(tmp_path, uring):
    root = os.fsencode(tmp_path)
    keys = ("path", "fd", "src", "dst", "dir_fd", "src_dir_fd", "dst_dir_fd")

    def guard(original, path_count, *args, **kwargs):
        paths = (
            *args[:path_count], *(kwargs[k] for k in keys if k in kwargs),
        )
        for path in paths:
            if isinstance(path, int):
                raise AssertionError(  # noqa: TRY004 (a test failure, not a type error)
                    f"userspace {original.__name__} on fd",
                )
            if path is not None:
                encoded = os.fsencode(path)
                if encoded == root or encoded.startswith(root + b"/"):
                    raise AssertionError(
                        f"userspace {original.__name__} on workload path",
                    )
        return original(*args, **kwargs)

    names = (
        "open", "close", "stat", "fstat", "truncate", "ftruncate", "mkdir",
        "unlink", "rmdir", "rename", "link", "symlink",
    )
    async with uring.AsyncioContext(sqpoll=False) as context:
        with ExitStack() as stack:
            for name in names:
                original = getattr(os, name)
                path_count = 2 if name in ("rename", "link", "symlink") else 1
                stack.enter_context(patch.object(
                    os, name, side_effect=partial(guard, original, path_count),
                ))
            stack.enter_context(patch.object(
                context.loop, "run_in_executor",
                side_effect=AssertionError("executor"),
            ))
            stack.enter_context(patch.object(
                asyncio, "to_thread", side_effect=AssertionError("to_thread"),
            ))

            # The guard itself works.
            assert stat.S_ISDIR(os.stat(tmp_path.parent).st_mode)
            with pytest.raises(AssertionError):
                os.stat(tmp_path)
            with pytest.raises(AssertionError):
                os.fstat(-1)

            directory = tmp_path / "native"
            await context.mkdir(directory)
            fd = await context.open(directory / "file", os.O_CREAT | os.O_RDWR)
            try:
                await context.truncate(fd, 4)
                assert (await context.stat(fd)).st_size == 4
            finally:
                await context.close_file(fd)
            await context.link(directory / "file", directory / "link")
            await context.symlink("file", directory / "symlink")
            await context.rename(directory / "file", directory / "renamed")
            await context.unlink(directory / "link")
            await context.unlink(directory / "symlink")
            await context.unlink(directory / "renamed")
            await context.rmdir(directory)
    assert not directory.exists()


@aiomisc.timeout(5)
async def test_dir_fd_relative_operations_and_statx_mask(tmp_path, uring):
    Operation = uring.Operation
    async with uring.AsyncioContext(sqpoll=False) as context:
        dfd = await context.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fd = await context.submit(Operation.openat(
                "relative", os.O_CREAT | os.O_RDWR, 0o600, dir_fd=dfd,
            ))
            await context.close_file(fd)

            result = await context.submit(
                Operation.statx("relative", dir_fd=dfd),
            )
            assert result.st_ino == (tmp_path / "relative").stat().st_ino

            await context.submit(Operation.renameat(
                "relative", "renamed", src_dir_fd=dfd, dst_dir_fd=dfd,
            ))
            await context.submit(Operation.linkat(
                "renamed", "linked", src_dir_fd=dfd, dst_dir_fd=dfd,
            ))
            await context.submit(
                Operation.symlinkat("renamed", "symlink", dir_fd=dfd),
            )
            await context.submit(Operation.mkdirat("child", dir_fd=dfd))
            await context.submit(Operation.unlinkat(
                "child", dir_fd=dfd, flags=AT_REMOVEDIR,
            ))

            # STATX__RESERVED is rejected by the kernel, not by caio.
            with pytest.raises(OSError) as caught:
                await context.submit(Operation.statx(
                    "renamed", dir_fd=dfd, mask=0x80000000,
                ))
            assert caught.value.errno == errno.EINVAL

            # A relative path needs a valid dir_fd; an absolute one does not.
            with pytest.raises(OSError) as caught:
                await context.submit(
                    Operation.openat("renamed", os.O_RDONLY, dir_fd=-1),
                )
            assert caught.value.errno == errno.EBADF
            absolute = await context.submit(Operation.openat(
                tmp_path / "renamed", os.O_RDONLY, dir_fd=-1,
            ))
            await context.close_file(absolute)

            for name in ("renamed", "linked", "symlink"):
                await context.submit(Operation.unlinkat(name, dir_fd=dfd))
        finally:
            await context.close_file(dfd)


@aiomisc.timeout(5)
async def test_cancelled_open_settles_and_closes_with_slot_held(
    tmp_path, paused_context,
):
    async with paused_context() as context:
        task = asyncio.create_task(
            context.open(tmp_path / "file", os.O_CREAT | os.O_RDWR),
        )
        opened = await context.submissions.get()
        assert opened.submitted
        assert not opened.completed

        for _ in range(3):
            task.cancel()
            await checkpoint()
            assert not task.done()
            assert context.semaphore.locked()

        # Cancellation before admission has no side effect.
        blocked = asyncio.create_task(
            context.open(tmp_path / "never", os.O_CREAT | os.O_RDWR),
        )
        await checkpoint()
        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked
        assert not (tmp_path / "never").exists()

        context.flush()
        cleanup = await context.submissions.get()
        assert cleanup.closes_file
        fd = opened.result
        assert os.fstat(fd).st_size == 0

        task.cancel()
        await checkpoint()
        assert not task.done()
        assert context.semaphore.locked()

        context.flush()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert_closed(fd)
        assert not context.semaphore.locked()


@aiomisc.timeout(5)
async def test_cleanup_rejection_does_not_transfer_or_leak_fd(
    tmp_path, paused_context,
):
    class RejectCleanupOnce(paused_context):
        rejected = False

        def _submit_op(self, op, future):
            if op.closes_file and not self.rejected:
                self.rejected = True
                raise OverflowError("injected full SQ")
            super()._submit_op(op, future)

    async with RejectCleanupOnce() as context:
        task = asyncio.create_task(
            context.open(tmp_path / "file", os.O_CREAT | os.O_RDWR),
        )
        opened = await context.submissions.get()
        task.cancel()
        await checkpoint()
        context.flush()

        cleanup = await context.submissions.get()
        assert context.rejected
        assert cleanup.closes_file
        assert context.semaphore.locked()
        with pytest.raises(RuntimeError):
            opened.get_value()

        context.flush()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert_closed(opened.result)


@aiomisc.timeout(5)
async def test_close_submission_rejection_preserves_ownership(tmp_path, uring):
    class RejectClose(uring.AsyncioContext):
        def _submit_op(self, op, future):
            if op.closes_file:
                raise OSError(errno.EOPNOTSUPP, "injected unsupported close")
            super()._submit_op(op, future)

    async with RejectClose(sqpoll=False) as context:
        fd = await context.open(tmp_path / "file", os.O_CREAT | os.O_RDWR)
        try:
            with pytest.raises(CloseError) as caught:
                await context.close_file(fd)
            assert caught.value.fd_closed is False
            assert caught.value.errno == errno.EOPNOTSUPP
            assert os.fstat(fd).st_size == 0
            assert not context.semaphore.locked()
        finally:
            os.close(fd)


@aiomisc.timeout(5)
async def test_fd_closed_tracks_admission_submission_and_completion(
    tmp_path, uring, paused_context,
):
    fd = os.open(tmp_path / "status", os.O_CREAT | os.O_RDWR, 0o600)
    async with paused_context() as context:
        op = uring.Operation.close(fd)
        await context.semaphore.acquire()
        task = asyncio.create_task(context.submit(op))
        await checkpoint()
        assert op.fd_closed is False

        context.semaphore.release()
        assert await context.submissions.get() is op
        assert op.submitted
        assert op.fd_closed is None

        task.cancel()
        await checkpoint()
        assert not task.done()
        assert op.fd_closed is None

        context.flush()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert op.fd_closed is True
        assert_closed(fd)


def test_fd_closed_is_defined_only_for_close_operations(uring):
    assert uring.Operation.close(-1).fd_closed is False
    with pytest.raises(AttributeError):
        _ = uring.Operation.statx(".").fd_closed


@aiomisc.timeout(5)
async def test_fd_closed_survives_close_error_construction_failure(
    tmp_path, uring,
):
    async with uring.AsyncioContext(sqpoll=False) as context:
        fd = await context.open(tmp_path / "file", os.O_CREAT | os.O_RDWR)
        op = uring.Operation.close(fd)
        closing = None
        original_submit = context._submit_op
        original_wait = context._wait_completion
        failure = MemoryError("constructing CloseError after CLOSE CQE")

        def observed_submit(op, future):
            nonlocal closing
            original_submit(op, future)
            if op.closes_file:
                closing = future

        async def failed_after_close(future):
            result = await original_wait(future)
            if future is closing:
                raise OSError(errno.EIO, "adapter failed after native CLOSE")
            return result

        with patch.object(
            context, "_submit_op", side_effect=observed_submit,
        ), patch.object(
            context, "_wait_completion", side_effect=failed_after_close,
        ), patch.object(
            uring, "CloseError", side_effect=failure,
        ), pytest.raises(MemoryError) as caught:
            await context.submit(op)
        assert caught.value is failure
        assert op.fd_closed is True
        assert_closed(fd)


@aiomisc.timeout(5)
async def test_fd_closed_survives_housekeeping_failure(tmp_path, uring):
    async with uring.AsyncioContext(sqpoll=False) as context:
        fd = await context.open(tmp_path / "file", os.O_CREAT | os.O_RDWR)
        op = uring.Operation.close(fd)
        original_release = context.semaphore.release
        failure = MemoryError("release bookkeeping after native CLOSE")

        def failed_release():
            original_release()
            raise failure

        with patch.object(
            context.semaphore, "release", side_effect=failed_release,
        ), pytest.raises(MemoryError) as caught:
            await context.submit(op)
        assert caught.value is failure
        assert op.fd_closed is True
        assert not context.semaphore.locked()
        assert_closed(fd)


@aiomisc.timeout(5)
async def test_close_on_closed_context_keeps_fd_for_retry(tmp_path, uring):
    context = uring.AsyncioContext(sqpoll=False)
    fd = await context.open(tmp_path / "file", os.O_CREAT | os.O_RDWR)
    context.close()

    op = uring.Operation.close(fd)
    with pytest.raises(RuntimeError):
        await context.submit(op)
    assert op.fd_closed is False
    assert os.fstat(fd).st_size == 0

    async with uring.AsyncioContext(sqpoll=False) as retry:
        retry_op = uring.Operation.close(fd)
        await retry.submit(retry_op)
    assert retry_op.fd_closed is True
    assert_closed(fd)


@aiomisc.timeout(5)
async def test_transient_flush_failure_does_not_resubmit_close(
    tmp_path, uring,
):
    class FailFlushOnce(uring.AsyncioContext):
        close_submissions = 0
        fail_flush = False

        def _submit_op(self, op, future):
            super()._submit_op(op, future)
            if op.closes_file:
                self.close_submissions += 1
                self.fail_flush = True

        def _on_submitted(self):
            if self.fail_flush:
                self.fail_flush = False
                raise OSError(errno.EINTR, "injected flush interruption")
            super()._on_submitted()

    async with FailFlushOnce(sqpoll=False) as context:
        fd = await context.open(tmp_path / "file", os.O_CREAT | os.O_RDWR)
        await context.close_file(fd)
        assert context.close_submissions == 1
        assert_closed(fd)


@pytest.mark.parametrize(
    ("failure", "expected_errno"),
    [
        (OSError(errno.EIO, "injected fatal flush failure"), errno.EIO),
        (MemoryError("injected CQ drain allocation failure"), errno.ENOMEM),
    ],
    ids=["oserror", "memoryerror"],
)
@aiomisc.timeout(5)
async def test_failure_after_submission_holds_slot_until_cqe(
    tmp_path, paused_context, failure, expected_errno,
):
    class FailFlush(paused_context):
        def _flush_filesystem(self):
            raise failure

    async with FailFlush() as context:
        fd = os.open(tmp_path / "file", os.O_CREAT | os.O_RDWR, 0o600)
        with pytest.raises(CloseError) as caught:
            await context.close_file(fd)
        operation = await context.submissions.get()

        # The SQE is published, so whether Linux released the fd is unknown
        # until its CQE arrives.
        assert caught.value.fd_closed is None
        assert caught.value.errno == expected_errno
        if isinstance(failure, MemoryError):
            assert caught.value.__cause__ is failure
        assert not operation.completed
        assert context.semaphore.locked()
        with pytest.raises(RuntimeError):
            context.close()

        context.flush()
        await context.semaphore.acquire()
        context.semaphore.release()
        assert operation.completed
        assert_closed(fd)
        assert not context.semaphore.locked()


@aiomisc.timeout(5)
async def test_close_allocation_failure_before_acceptance_preserves_fd(
    tmp_path, uring,
):
    class FailAllocation(uring.AsyncioContext):
        def _submit_op(self, op, future):
            if op.closes_file:
                raise MemoryError("injected pre-submit allocation failure")
            super()._submit_op(op, future)

    async with FailAllocation(sqpoll=False) as context:
        fd = await context.open(tmp_path / "file", os.O_CREAT | os.O_RDWR)
        try:
            with pytest.raises(CloseError) as caught:
                await context.close_file(fd)
            assert caught.value.fd_closed is False
            assert caught.value.errno == errno.ENOMEM
            assert isinstance(caught.value.__cause__, MemoryError)
            assert os.fstat(fd).st_size == 0
            assert not context.semaphore.locked()
        finally:
            os.close(fd)


@pytest.mark.parametrize(
    ("failure", "expected_errno"),
    [
        (MemoryError("injected after CLOSE CQE"), errno.ENOMEM),
        (SystemExit("injected adapter failure"), errno.EIO),
    ],
    ids=["memoryerror", "systemexit"],
)
@aiomisc.timeout(5)
async def test_adapter_failure_after_cqe_reports_release(
    tmp_path, uring, failure, expected_errno,
):
    class FailAfterCompletion(uring.AsyncioContext):
        close_operation = None

        def _submit_op(self, op, future):
            super()._submit_op(op, future)
            if op.closes_file:
                self.close_operation = op

        async def _wait_completion(self, future):
            cancelled = await super()._wait_completion(future)
            operation = self.close_operation
            if operation is not None and operation.completed:
                raise failure
            return cancelled

    async with FailAfterCompletion(sqpoll=False) as context:
        fd = await context.open(tmp_path / "file", os.O_CREAT | os.O_RDWR)
        with pytest.raises(CloseError) as caught:
            await context.close_file(fd)
        assert caught.value.fd_closed is True
        assert caught.value.errno == expected_errno
        assert caught.value.__cause__ is failure
        assert context.close_operation.completed
        assert_closed(fd)

        # Nothing may close the number again once Linux reuses it.
        reused = os.open(tmp_path / "replacement", os.O_CREAT | os.O_RDWR)
        try:
            assert reused == fd
            context.close_operation = None
            gc.collect()
            assert os.fstat(reused).st_size == 0
        finally:
            os.close(reused)


@aiomisc.timeout(5)
async def test_cancelled_close_settles_and_spares_reused_fd(
    tmp_path, paused_context,
):
    fd = os.open(tmp_path / "file", os.O_CREAT | os.O_RDWR, 0o600)
    async with paused_context() as context:
        task = asyncio.create_task(context.close_file(fd))
        operation = await context.submissions.get()
        for _ in range(3):
            task.cancel()
            await checkpoint()
            assert not task.done()

        context.flush()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert operation.completed
        assert_closed(fd)

        reused = os.open(tmp_path / "replacement", os.O_CREAT | os.O_RDWR)
        try:
            assert reused == fd
            assert not task.cancel()
            assert os.fstat(reused).st_size == 0
        finally:
            os.close(reused)


@aiomisc.timeout(5)
async def test_close_cancelled_before_admission_still_closes(
    tmp_path, paused_context,
):
    fd = os.open(tmp_path / "file", os.O_CREAT | os.O_RDWR, 0o600)
    async with paused_context() as context:
        await context.semaphore.acquire()
        task = asyncio.create_task(context.close_file(fd))
        await checkpoint()
        for _ in range(3):
            task.cancel()
            await checkpoint()
            assert not task.done()
        assert context.submissions.empty()

        context.semaphore.release()
        operation = await context.submissions.get()
        assert operation.closes_file
        context.flush()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert_closed(fd)


@aiomisc.timeout(5)
async def test_cancelled_metadata_operation_is_not_rolled_back(
    tmp_path, paused_context,
):
    async with paused_context() as context:
        task = asyncio.create_task(context.mkdir(tmp_path / "created"))
        operation = await context.submissions.get()
        for _ in range(2):
            task.cancel()
            await checkpoint()
        assert not task.done()
        assert not operation.completed

        context.flush()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (tmp_path / "created").is_dir()
        assert operation.completed
        assert not context.semaphore.locked()


@aiomisc.timeout(5)
async def test_ring_close_rejects_inflight_and_is_idempotent(
    tmp_path, paused_context,
):
    context = paused_context()
    task = asyncio.create_task(context.stat(tmp_path))
    operation = await context.submissions.get()
    ring_fd = context.context.fileno

    with pytest.raises(RuntimeError):
        context.close()
    with pytest.raises(RuntimeError):
        context.context.close()
    assert not operation.completed

    context.flush()
    assert stat.S_ISDIR((await task).st_mode)
    context.close()
    context.close()
    assert_closed(ring_fd)

    with pytest.raises(RuntimeError):
        await context.stat(tmp_path)
    with pytest.raises(RuntimeError):
        context.context.process_events()


def test_low_level_open_ownership(tmp_path, uring):
    context = uring.Context(max_requests=1, sqpoll=False)
    try:
        # A completed open that is never claimed closes its fd on drop.
        for _ in range(20):
            operation = uring.Operation.openat(
                tmp_path / "file", os.O_CREAT | os.O_RDWR, 0o600,
            )
            assert context.submit(operation) == 1
            wait_for_completion(context, operation)
            fd = operation.result
            reference = weakref.ref(operation)
            del operation
            gc.collect()
            assert reference() is None
            assert_closed(fd)

        # get_value() transfers ownership exactly once.
        operation = uring.Operation.openat(tmp_path / "file", os.O_RDWR)
        assert context.submit(operation) == 1
        wait_for_completion(context, operation)
        fd = operation.get_value()
        with pytest.raises(RuntimeError):
            operation.get_value()
        del operation
        gc.collect()
        assert os.fstat(fd).st_size == 0

        closing = uring.Operation.close(fd)
        context.submit(closing)
        wait_for_completion(context, closing)
        assert closing.get_value() is None
        assert_closed(fd)
    finally:
        context.close()


def test_low_level_drop_before_cqe_keeps_operation_alive(tmp_path, uring):
    context = uring.Context(max_requests=1, sqpoll=False)
    try:
        completed = []
        operation = uring.Operation.openat(
            tmp_path / "file", os.O_CREAT | os.O_RDWR, 0o600,
        )
        operation.set_callback(completed.append)
        reference = weakref.ref(operation)
        context.submit(operation)
        del operation
        gc.collect()
        # The ring keeps the operation (and its path buffer) alive.
        assert reference() is not None

        context.flush()
        if not completed:
            context.process_events(min_requests=1, timeout=5)
        assert len(completed) == 1
        assert completed[0] >= 0
        gc.collect()
        assert reference() is None
        assert_closed(completed[0])
    finally:
        context.close()
