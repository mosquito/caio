from collections.abc import Callable
from typing import Any

from .abstract import AbstractContext, AbstractOperation

# noinspection PyPropertyDefinition
class Context(AbstractContext):
    def __init__(self, max_requests: int = 512, pool_size=8): ...

    @property
    def pool_size(self) -> int: ...

    def close(self) -> None: ...


# noinspection PyPropertyDefinition
class Operation(AbstractOperation):
    @classmethod
    def read(
        cls, nbytes: int, fd: int, offset: int, priority=0
    ) -> AbstractOperation: ...

    @classmethod
    def write(
        cls, payload_bytes: bytes,
        fd: int, offset: int, priority=0,
    ) -> AbstractOperation: ...

    @classmethod
    def fsync(cls, fd: int, priority=0) -> AbstractOperation: ...

    @classmethod
    def fdsync(cls, fd: int, priority=0) -> AbstractOperation: ...

    def get_value(self) -> bytes | int | None: ...

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

    def set_callback(self, callback: Callable[[int], Any]) -> bool: ...
