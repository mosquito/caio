import abc
from typing import Optional, Union, Callable, Any


class ContextBase(abc.ABC):
    @abc.abstractproperty
    def max_requests(self) -> int:
        raise NotImplementedError

    @abc.abstractmethod
    def submit(self, *aio_operations) -> int:
        raise NotImplementedError(aio_operations)


class OperationBase(abc.ABC):
    @abc.abstractclassmethod
    def read(cls, nbytes: int, fd: int,
             offset: int, priority=0) -> "OperationBase":
        """
        Creates a new instance of AIOOperation on read mode.
        """

    @abc.abstractclassmethod
    def write(
        cls, payload_bytes: bytes,
        fd: int, offset: int, priority=0) -> "OperationBase":
        """
        Creates a new instance of AIOOperation on write mode.
        """
        raise NotImplementedError

    @abc.abstractclassmethod
    def fsync(cls, fd: int, priority=0) -> "OperationBase":
        """
        Creates a new instance of AIOOperation on fsync mode.
        """
        raise NotImplementedError

    @abc.abstractclassmethod
    def fdsync(cls, fd: int, priority=0) -> "OperationBase":

        """
        Creates a new instance of AIOOperation on fdsync mode.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_value(self) -> Union[bytes, int]:
        """
        Method returns a bytes value of AIOOperation's result or None.
        """
        raise NotImplementedError

    @abc.abstractproperty
    def fileno(self) -> int:
        raise NotImplementedError

    @abc.abstractproperty
    def offset(self) -> int:
        raise NotImplementedError

    @abc.abstractproperty
    def payload(self) -> Optional[Union[bytes, memoryview]]:
        raise NotImplementedError

    @abc.abstractproperty
    def nbytes(self) -> int:
        raise NotImplementedError

    @abc.abstractmethod
    def set_callback(self, callback: Callable[[int], Any]) -> bool:
        raise NotImplementedError
