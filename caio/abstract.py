import abc
from collections.abc import Callable
from typing import Any


class CloseError(OSError):
    """A failed native close with an explicit descriptor-ownership outcome.

    fd_closed=False means no close SQE was accepted. True confirms a
    successful close CQE before an adapter failure. None means a CQE or
    submission failure cannot prove whether Linux released the fd: owners
    must discard the numeric fd, never retry it, and surface the error.
    Owners that must know the outcome even if this exception cannot be
    allocated should submit their own Operation.close(fd) and read its
    fd_closed attribute.
    """

    def __init__(self, error: OSError, *, fd_closed: bool | None):
        super().__init__(*error.args)
        self.fd_closed = fd_closed


class AbstractContext(abc.ABC):
    @property
    def max_requests(self) -> int:
        raise NotImplementedError

    def submit(self, *aio_operations) -> int:
        raise NotImplementedError(aio_operations)

    def cancel(self, *aio_operations) -> int:
        raise NotImplementedError(aio_operations)


class AbstractOperation(abc.ABC):
    @classmethod
    @abc.abstractmethod
    def read(
        cls, nbytes: int, fd: int,
        offset: int, priority=0,
    ) -> "AbstractOperation":
        """
        Creates a new instance of AIOOperation on read mode.
        """
        raise NotImplementedError

    @classmethod
    @abc.abstractmethod
    def write(
        cls, payload_bytes: bytes,
        fd: int, offset: int, priority=0,
    ) -> "AbstractOperation":
        """
        Creates a new instance of AIOOperation on write mode.
        """
        raise NotImplementedError

    @classmethod
    @abc.abstractmethod
    def fsync(cls, fd: int, priority=0) -> "AbstractOperation":
        """
        Creates a new instance of AIOOperation on fsync mode.
        """
        raise NotImplementedError

    @classmethod
    @abc.abstractmethod
    def fdsync(cls, fd: int, priority=0) -> "AbstractOperation":

        """
        Creates a new instance of AIOOperation on fdsync mode.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_value(self) -> bytes | int | None:
        """
        Method returns a bytes value of AIOOperation's result or None.
        """
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def fileno(self) -> int:
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def offset(self) -> int:
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def payload(self) -> bytes | memoryview | None:
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def nbytes(self) -> int:
        raise NotImplementedError

    @abc.abstractmethod
    def set_callback(self, callback: Callable[[int], Any]) -> bool:
        raise NotImplementedError
