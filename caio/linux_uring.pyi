from collections.abc import Callable
from os import PathLike
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

    def close(self) -> None: ...

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

    @classmethod
    def openat(
        cls, path: str | bytes | PathLike[str] | PathLike[bytes], flags: int,
        mode: int = 0o666, *, dir_fd: int = -100,
    ) -> Operation: ...

    @classmethod
    def close(cls, fd: int) -> Operation: ...

    @classmethod
    def statx(
        cls, path: str | bytes | PathLike[str] | PathLike[bytes], *,
        dir_fd: int = -100, flags: int = 0, mask: int = 0x7ff,
    ) -> Operation: ...

    @classmethod
    def ftruncate(cls, fd: int, length: int = 0) -> Operation: ...

    @classmethod
    def mkdirat(
        cls, path: str | bytes | PathLike[str] | PathLike[bytes], mode: int = 0o777,
        *, dir_fd: int = -100,
    ) -> Operation: ...

    @classmethod
    def unlinkat(
        cls, path: str | bytes | PathLike[str] | PathLike[bytes], *,
        dir_fd: int = -100, flags: int = 0,
    ) -> Operation: ...

    @classmethod
    def renameat(
        cls, src: str | bytes | PathLike[str] | PathLike[bytes],
        dst: str | bytes | PathLike[str] | PathLike[bytes], *,
        src_dir_fd: int = -100, dst_dir_fd: int = -100, flags: int = 0,
    ) -> Operation: ...

    @classmethod
    def linkat(
        cls, src: str | bytes | PathLike[str] | PathLike[bytes],
        dst: str | bytes | PathLike[str] | PathLike[bytes], *,
        src_dir_fd: int = -100, dst_dir_fd: int = -100, flags: int = 0,
    ) -> Operation: ...

    @classmethod
    def symlinkat(
        cls, src: str | bytes | PathLike[str] | PathLike[bytes],
        dst: str | bytes | PathLike[str] | PathLike[bytes], *, dir_fd: int = -100,
    ) -> Operation: ...

    def get_value(self) -> Any: ...

    def _take_fd_ownership(self) -> None: ...

    @property
    def is_filesystem(self) -> bool: ...

    @property
    def opens_file(self) -> bool: ...

    @property
    def closes_file(self) -> bool: ...

    @property
    def submitted(self) -> bool: ...

    @property
    def completed(self) -> bool: ...

    @property
    def fd_closed(self) -> bool | None: ...

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
