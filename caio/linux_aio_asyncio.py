from .asyncio_base import AsyncioContextBase
from .linux_aio import Context, Operation


class AsyncioContext(AsyncioContextBase):
    OPERATION_CLASS = Operation
    CONTEXT_CLASS = Context

    def _create_context(self, max_requests):
        context = super()._create_context(max_requests)
        self.loop.add_reader(context.fileno, self._on_read_event)
        return context

    def _on_done(self, future, result):
        """
        Allow to set result directly.
        Cause process_events running in the same thread
        """
        if future.done():
            return
        future.set_result(True)

    def _destroy_context(self):
        self.loop.remove_reader(self.context.fileno)

    def _on_read_event(self):
        # poll() raises BlockingIOError whenever the eventfd's counter
        # reads as zero - possible even right after epoll reported this fd
        # readable (e.g. the kernel coalesces multiple completions into one
        # counter increment, and the ones beyond the first can still be
        # landing at the moment poll() runs). Letting that exception escape
        # used to skip process_events() entirely for this wakeup - draining
        # doesn't depend on the eventfd count, so a stale/absent counter is
        # never a reason to skip it, only silently stranding whatever was
        # already completed with no other event left to wake this Context
        # up again. Confirmed to actually hang a real (non-tmpfs) benchmark
        # run this way for linux_uring's identical pattern - inline tmpfs
        # completions never exercise this path at all.
        try:
            self.context.poll()
        except BlockingIOError:
            pass
        while self.context.process_events():
            pass
