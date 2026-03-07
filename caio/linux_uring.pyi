from typing import Union, Optional, Callable, Any

from .abstract import AbstractContext, AbstractOperation


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


# noinspection PyPropertyDefinition
class Operation(AbstractOperation):
    @classmethod
    def read(
        cls, nbytes: int, fd: int, offset: int, priority: int = 0,
    ) -> "Operation": ...

    @classmethod
    def write(
        cls, payload_bytes: bytes, fd: int, offset: int, priority: int = 0,
    ) -> "Operation": ...

    @classmethod
    def fsync(cls, fd: int, priority: int = 0) -> "Operation": ...

    @classmethod
    def fdsync(cls, fd: int, priority: int = 0) -> "Operation": ...

    def get_value(self) -> Union[bytes, int]: ...

    def set_callback(self, callback: Callable[[int], Any]) -> bool: ...

    @property
    def fileno(self) -> int: ...

    @property
    def offset(self) -> int: ...

    @property
    def payload(self) -> Optional[Union[bytes, memoryview]]: ...

    @property
    def nbytes(self) -> int: ...

    @property
    def result(self) -> int: ...

    @property
    def error(self) -> int: ...
