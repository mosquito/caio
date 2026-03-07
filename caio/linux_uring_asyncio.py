from .asyncio_base import AsyncioContextBase
from .linux_uring import Context, Operation


class AsyncioContext(AsyncioContextBase):
    OPERATION_CLASS = Operation
    CONTEXT_CLASS = Context

    def _create_context(self, max_requests):
        context = super()._create_context(max_requests)
        self.loop.add_reader(context.fileno, self._on_read_event)
        return context

    def _on_done(self, future, result):
        if future.done():
            return
        future.set_result(True)

    def _destroy_context(self):
        self.loop.remove_reader(self.context.fileno)

    def _on_submitted(self):
        # Flush immediately after every submit.
        #
        # Non-SQPOLL (default): flush() calls io_uring_enter() which completes
        # page-cache ops inline, then drain_cq() fires futures *before* the
        # caller reaches `await future` — so the coroutine never suspends for
        # those ops.  Truly async ops (real disk) leave the future unset; the
        # coroutine suspends and the eventfd wakes it when the kernel is done.
        #
        # SQPOLL: flush() wakes the kernel thread if sleeping, then drain_cq().
        # Same "fast path" benefit when the thread has already completed the op.
        self.context.flush()

    def _on_read_event(self):
        """Handle completions signalled via the eventfd."""
        self.context.poll()
        while self.context.process_events():
            pass
