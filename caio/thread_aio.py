import os
import threading
from collections import namedtuple, deque
from typing import Optional, Union, Callable, Any

try:
    from queue import SimpleQueue
except ImportError:
    from queue import Queue as SimpleQueue


class Context:
    def __init__(self, max_requests: int = 32):
        self.max_requests = max_requests
        self.pool = set()
        self.queue = SimpleQueue()
        self.results = deque()
        self.operations = set()

        for _ in range(max_requests):
            thread = threading.Thread(target=self._worker)
            self.pool.add(thread)
            thread.start()

    def _worker(self):
        while True:
            operation = self.queue.get()

            if operation is None:
                return

            try:
                pass
            except:
                pass

    def submit(self, *operations) -> int:
        for operation in operations:
            self.queue.put_nowait(operation)

        return len(operations)

    def read(self, *aio_operations) -> int:
        pass

    def poll(self) -> int:
        pass

    def process_events(
            self, max_events: int = 512, min_events: int = 0, timeout: int = 0
    ) -> int:
        pass

    def __del__(self):
        for _ in range(self.max_requests):
            self.queue.put_nowait(None)


OperationBase = namedtuple("OperationBase", (
    "fileno",
    "operation",
    "priority",
    "args",
    "offset",
))


class Operation(OperationBase):
    def __new__(cls, operation, fd, priority, *args, offset=None):
        self = super(Operation, cls).__new__(
            cls,
            fileno=fd,
            operation=operation,
            priority=priority,
            args=args,
            offset=offset,
        )

        return self

    def __init__(self, operation, fd, priority, *args, offset=None):
        self.buffer = None
        self.callback = None
        self.nbytes = None

    @classmethod
    def read(cls, nbytes: int, fd: int, offset: int, priority=0):
        """
        Creates a new instance of AIOOperation on read mode.
        """
        return cls(os.read, fd, priority, nbytes, offset)

    @classmethod
    def write(cls, payload_bytes: bytes, fd: int, offset: int, priority=0):
        """
        Creates a new instance of AIOOperation on write mode.
        """
        return cls(os.write, fd, priority, payload_bytes, offset=offset)

    @classmethod
    def fsync(cls, fd: int, priority=0):

        """
        Creates a new instance of AIOOperation on fsync mode.
        """
        return cls(os.fsync, fd, priority)

    @classmethod
    def fdsync(cls, fd: int, priority=0):

        """
        Creates a new instance of AIOOperation on fdsync mode.
        """
        return cls(os.fsync, fd, priority)

    def get_value(self) -> Optional[bytes]:
        """
        Method returns a bytes value of AIOOperation's result or None.
        """
        return self.buffer

    @property
    def payload(self) -> Optional[Union[bytes, memoryview]]:
        ...

    def set_callback(self, callback: Callable[[int], Any]) -> bool:
        self.callback = callback
        return True
