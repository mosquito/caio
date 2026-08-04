from collections.abc import Callable
from typing import Any

from .abstract import AbstractContext, AbstractOperation

# True if this kernel/process can actually negotiate IORING_SETUP_SQPOLL -
# probed once at import time (see linux_uring.c's PyInit_linux_uring).
SQPOLL_ALLOWED: bool

# noinspection PyPropertyDefinition
class Context(AbstractContext):
    def __init__(self, max_requests: int = 32, sqpoll: bool = False): ...

    @property
    def fileno(self) -> int: ...

    def poll(self) -> int: ...

    def process_events(
        self,
        max_requests: int = 512,
        min_requests: int = 0,
        timeout: int = 0,
    ) -> int: ...

    def flush(self) -> int: ...

    @property
    def sqpoll(self) -> bool: ...


# noinspection PyPropertyDefinition
class Operation(AbstractOperation):
    @classmethod
    def read(
        cls, nbytes: int, fd: int, offset: int, priority: int = 0,
    ) -> Operation: ...

    @classmethod
    def write(
        cls, payload_bytes: bytes, fd: int, offset: int, priority: int = 0,
    ) -> Operation: ...

    @classmethod
    def fsync(cls, fd: int, priority: int = 0) -> Operation: ...

    @classmethod
    def fdsync(cls, fd: int, priority: int = 0) -> Operation: ...

    def get_value(self) -> bytes | int | None: ...

    def set_callback(self, callback: Callable[[int], Any]) -> bool: ...

    @property
    def fileno(self) -> int: ...

    @property
    def offset(self) -> int: ...

    @property
    def payload(self) -> bytes | memoryview | None: ...

    @property
    def nbytes(self) -> int: ...

    @property
    def result(self) -> int: ...

    @property
    def error(self) -> int: ...

    @property
    def context(self) -> AbstractContext | None: ...
