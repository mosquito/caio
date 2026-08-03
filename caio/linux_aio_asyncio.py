from .asyncio_base import AsyncioContextBase
from .linux_aio import Context, Operation


class AsyncioContext(AsyncioContextBase):
    OPERATION_CLASS = Operation
    CONTEXT_CLASS = Context

    def _create_context(self, max_requests):
        context = super()._create_context(max_requests)
        self.loop.add_reader(context.fileno, self._on_read_event)
        self._pending = []
        self._submit_scheduled = False
        return context

    def _submit_op(self, op, future):
        if not self.deferred:
            super()._submit_op(op, future)
            return
        # Context.submit() already accepts *ops and builds one iocbpp array
        # for the whole batch - call_soon() defers to the next _run_once()
        # pass, after every currently-ready submit() has queued its op, so
        # one submit() call handles all of them together.
        self._pending.append((op, future))
        if not self._submit_scheduled:
            self._submit_scheduled = True
            self.loop.call_soon(self._deferred_submit)

    def _deferred_submit(self):
        self._submit_scheduled = False
        pending, self._pending = self._pending, []
        # Already cancelled while waiting here - never reached the kernel,
        # nothing to submit or cancel for these.
        pending = [(op, fut) for op, fut in pending if not fut.done()]
        if not pending:
            return
        ops = [op for op, _ in pending]
        try:
            accepted = self.context.submit(*ops)
        except BaseException as exc:  # noqa: BLE001 (must isolate any submit()-time exception, including SystemExit/KeyboardInterrupt, since this runs as a bare call_soon callback)
            # submit() itself can raise synchronously (e.g. a bad fd in the
            # batch) - this runs as a bare call_soon callback, not inside
            # any of the pending coroutines, so letting it escape here
            # would just get logged by asyncio's default handler and leave
            # every future in the batch unresolved forever. Every op in
            # this batch failed together (io_submit() rejects the whole
            # call on this kind of error, not just a prefix).
            for op, fut in pending:
                if not fut.done():
                    fut.set_exception(exc)
            return
        # io_submit() accepts a prefix; anything past `accepted` failed.
        for op, fut in pending[accepted:]:
            if not fut.done():
                fut.set_exception(OSError("Operation was not submitted"))

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
