import asyncio
import errno
import os
from functools import partial

from .abstract import CloseError
from .asyncio_base import AsyncioContextBase
from .linux_uring import SQPOLL_ALLOWED, Context, Operation


class AsyncioContext(AsyncioContextBase):
    FILE_OPERATIONS_VERSION = 1
    OPERATION_CLASS = Operation
    CONTEXT_CLASS = Context

    def _create_context(self, max_requests, **kwargs):
        # SQPOLL_ALLOWED reflects a real kernel/capability probe done once
        # at import time (see linux_uring.c) - default to it rather than
        # to sqpoll=False, but let an explicit caller kwarg win either way.
        kwargs.setdefault("sqpoll", SQPOLL_ALLOWED)
        context = super()._create_context(max_requests, **kwargs)
        self.loop.add_reader(context.fileno, self._on_read_event)
        self._flush_scheduled = False
        self._flush_handle = None
        self._closed = False
        self._filesystem_operations = 0
        return context

    def _on_done(self, future, result):
        if future.done():
            return
        future.set_result(True)

    def _destroy_context(self):
        if self._closed:
            return
        if self._filesystem_operations:
            raise RuntimeError("cannot close context with outstanding filesystem operations")
        fd = self.context.fileno
        self.context.close()
        self.loop.remove_reader(fd)
        self._closed = True
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        self._flush_scheduled = False

    async def submit(self, op):
        if self._closed:
            raise RuntimeError("context is closed")
        if isinstance(op, Operation) and op.is_filesystem:
            return await self._submit_filesystem(op)
        return await super().submit(op)

    async def _wait_completion(self, future):
        """Wait for true completion without cancelling its future, even on repeated cancel()."""
        cancelled = None
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError as error:
                if future.cancelled():
                    raise
                if cancelled is None:
                    cancelled = error
        future.result()
        return cancelled

    def _start_filesystem(self, op, future):
        op.set_callback(partial(self._on_done, future))
        self._submit_op(op, future)

    def _flush_filesystem(self):
        try:
            self._on_submitted()
        except (OSError, SystemError, RuntimeError):
            # A published SQE must still settle. flush retries only pending
            # SQEs, never re-submits the operation (especially CLOSE).
            self.context.flush()

    def _start_open_cleanup(self, opened, cleanup, future):
        cleanup.set_callback(partial(self._on_done, future))
        try:
            self._submit_op(cleanup, future)
        except (OSError, OverflowError):
            if not cleanup.submitted:
                self.context.flush()
                self._submit_op(cleanup, future)
        # Transfer only after acceptance, without a Python int allocation.
        opened._take_fd_ownership()

    async def _submit_filesystem(self, op):
        """Before admission cancellation has no effect, except for close.

        After submission all filesystem operations settle before cancellation
        propagates. Side effects may have happened; cancellation is not rollback.
        A successful cancelled open is closed through io_uring before returning.
        Close also settles admission so cancellation cannot leave ownership
        ambiguous once close_file's coroutine has started. CQE errors take
        precedence over cancellation and are never silently retried.
        """
        self._filesystem_operations += 1
        admitted = False
        future = None
        active_op = op
        try:
            cancelled = None
            if op.closes_file:
                admission = self.loop.create_task(self.semaphore.acquire())
                cancelled = await self._wait_completion(admission)
            else:
                await self.semaphore.acquire()
            admitted = True
            if self._closed:
                raise RuntimeError("context is closed")
            future = self.loop.create_future()
            self._start_filesystem(op, future)
            self._flush_filesystem()
            completion_cancel = await self._wait_completion(future)
            cancelled = cancelled or completion_cancel
            if cancelled is not None and op.opens_file and op.error == 0:
                cleanup = Operation.close(op.result)
                future = self.loop.create_future()
                active_op = cleanup
                self._start_open_cleanup(op, cleanup, future)
                self._flush_filesystem()
                await self._wait_completion(future)
                cleanup.get_value()
                raise cancelled
            value = op.get_value()
            if cancelled is not None:
                raise cancelled
            return value
        except BaseException as error:
            if not active_op.closes_file:
                raise
            fd_closed = active_op.fd_closed
            if isinstance(error, asyncio.CancelledError) and fd_closed is True:
                raise
            if isinstance(error, OSError):
                close_error = error
            else:
                error_number = errno.ENOMEM if isinstance(error, MemoryError) else errno.EIO
                close_error = OSError(error_number, type(error).__name__)
            raise CloseError(close_error, fd_closed=fd_closed) from error
        finally:
            if admitted and active_op.submitted and future is not None and not future.done():
                # A fatal flush failure cannot retire a published SQE. Keep
                # admission and the context alive until its real CQE arrives.
                future.add_done_callback(self._release_filesystem_slot)
            else:
                try:
                    if admitted:
                        self.semaphore.release()
                finally:
                    self._filesystem_operations -= 1

    def _release_filesystem_slot(self, future):
        try:
            self.semaphore.release()
        finally:
            self._filesystem_operations -= 1

    async def open(
        self, path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int, mode: int = 0o666,
    ) -> int:
        """Return a non-inheritable fd owned by the caller; cancelled opens cannot leak it."""
        return await self.submit(Operation.openat(path, flags, mode))

    async def close_file(self, fd: int) -> None:
        """Close exactly once, settling completion before propagating cancellation.

        A task cancelled before this coroutine first runs has not requested a
        close. Owners doing cleanup should shield a started cleanup task.
        Failures raise CloseError. To learn the descriptor's disposition even
        when that exception cannot be allocated, submit Operation.close(fd)
        and read its fd_closed attribute afterwards: retain the descriptor
        only for False; True confirms release and None requires discarding it.
        """
        await self.submit(Operation.close(fd))

    async def stat(
        self, path_or_fd: str | bytes | os.PathLike[str] | os.PathLike[bytes] | int,
        *, follow_symlinks: bool = True,
    ) -> os.stat_result:
        if isinstance(path_or_fd, int):
            if path_or_fd < 0:
                raise OSError(errno.EBADF, os.strerror(errno.EBADF))
            if not follow_symlinks:
                raise ValueError("stat: cannot use fd and follow_symlinks together")
            op = Operation.statx(b"", dir_fd=path_or_fd, flags=0x1000)  # AT_EMPTY_PATH
        else:
            flags = 0 if follow_symlinks else 0x100  # AT_SYMLINK_NOFOLLOW
            op = Operation.statx(path_or_fd, flags=flags)
        return await self.submit(op)

    async def truncate(self, fd: int, length: int = 0) -> None:
        """Truncate through IORING_OP_FTRUNCATE (Linux 6.9+); no fallback."""
        await self.submit(Operation.ftruncate(fd, length))

    async def mkdir(
        self, path: str | bytes | os.PathLike[str] | os.PathLike[bytes], mode: int = 0o777,
    ) -> None:
        await self.submit(Operation.mkdirat(path, mode))

    async def unlink(self, path: str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> None:
        await self.submit(Operation.unlinkat(path))

    async def rmdir(self, path: str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> None:
        await self.submit(Operation.unlinkat(path, flags=0x200))  # AT_REMOVEDIR

    async def rename(
        self, src: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        dst: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> None:
        await self.submit(Operation.renameat(src, dst))

    async def link(
        self, src: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        dst: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> None:
        await self.submit(Operation.linkat(src, dst))

    async def symlink(
        self, src: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        dst: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> None:
        await self.submit(Operation.symlinkat(src, dst))

    def _on_submitted(self):
        # Non-SQPOLL flush() completes page-cache ops inline (drain_cq fires
        # futures before `await future` even suspends); SQPOLL just wakes
        # the kernel thread if it's asleep - and only when it's actually
        # gone idle, so eager per-op flush() is already close to syscall-
        # free there. Batching doesn't reduce a cost that's mostly already
        # gone - measured no meaningful difference either way - so ignore
        # deferred whenever the kernel actually negotiated SQPOLL (not
        # just what was requested - EPERM/EINVAL can silently fall back to
        # a plain ring, see linux_uring.c's flag_table_sqpoll).
        if not self.deferred or self.context.sqpoll:
            self.context.flush()
            return
        # Nothing between submit()'s isinstance check and here ever awaits,
        # so N concurrently-scheduled submits used to each flush their own
        # single SQE back to back - no batching despite N being ready at
        # once. call_soon() defers to the next _run_once() pass, after all
        # of them have written their SQE, so one flush() covers the whole
        # batch - at the cost of one extra event-loop round-trip for a lone
        # unbatched op.
        if not self._flush_scheduled:
            self._flush_scheduled = True
            self._flush_handle = self.loop.call_soon(self._deferred_flush)

    def _deferred_flush(self):
        self._flush_scheduled = False
        self._flush_handle = None
        if not self._closed:
            self.context.flush()

    def _on_read_event(self):
        """Handle completions signalled via the eventfd."""
        # poll() raises BlockingIOError whenever the eventfd's counter
        # reads as zero - possible even right after epoll reported this fd
        # readable (e.g. the kernel coalesces multiple completions into one
        # counter increment, and the ones beyond the first can still be
        # landing in the CQ ring at the moment poll() runs). Letting that
        # exception escape used to skip process_events() entirely for this
        # wakeup - draining is independent of the eventfd count (it reads
        # the CQ ring directly), so a stale/absent counter is never a
        # reason to skip it, only silently stranding whatever was already
        # completed with no other event left to wake this Context up
        # again. Confirmed to actually hang a real (non-tmpfs) benchmark
        # run this way - inline tmpfs completions never exercise this path
        # at all, since they never reach the eventfd in the first place.
        try:
            self.context.poll()
        except BlockingIOError:
            pass
        while self.context.process_events():
            pass
