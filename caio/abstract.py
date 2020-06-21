import abc
from typing import Any, Callable, Optional, Union


class AbstractContext(abc.ABC):
    @property
    def max_requests(self) -> int:
        raise NotImplementedError

    def submit(self, *aio_operations) -> int:
        raise NotImplementedError(aio_operations)


class AbstractOperation(abc.ABC):
    @classmethod
    def read(
        cls, nbytes: int, fd: int,
        offset: int, priority=0,
    ) -> "AbstractOperation":
        """
        Creates a new instance of AIOOperation on read mode.
        """
        raise NotImplementedError

    @classmethod
    def write(
        cls, payload_bytes: bytes,
        fd: int, offset: int, priority=0,
    ) -> "AbstractOperation":
        """
        Creates a new instance of AIOOperation on write mode.
        """
        raise NotImplementedError

    @classmethod
    def fsync(cls, fd: int, priority=0) -> "AbstractOperation":
        """
        Creates a new instance of AIOOperation on fsync mode.
        """
        raise NotImplementedError

    @classmethod
    def fdsync(cls, fd: int, priority=0) -> "AbstractOperation":

        """
        Creates a new instance of AIOOperation on fdsync mode.
        """
        raise NotImplementedError

    def get_value(self) -> Union[bytes, int]:
        """
        Method returns a bytes value of AIOOperation's result or None.
        """
        raise NotImplementedError

    def fileno(self) -> int:
        raise NotImplementedError

    def offset(self) -> int:
        raise NotImplementedError

    def payload(self) -> Optional[Union[bytes, memoryview]]:
        raise NotImplementedError

    def nbytes(self) -> int:
        raise NotImplementedError

    def set_callback(self, callback: Callable[[int], Any]) -> bool:
        raise NotImplementedError
