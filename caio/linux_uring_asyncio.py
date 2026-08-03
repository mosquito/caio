from .asyncio_base import AsyncioContextBase
from .linux_uring import SQPOLL_ALLOWED, Context, Operation


class AsyncioContext(AsyncioContextBase):
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
        return context

    def _on_done(self, future, result):
        if future.done():
            return
        future.set_result(True)

    def _destroy_context(self):
        self.loop.remove_reader(self.context.fileno)

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
            self.loop.call_soon(self._deferred_flush)

    def _deferred_flush(self):
        self._flush_scheduled = False
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
