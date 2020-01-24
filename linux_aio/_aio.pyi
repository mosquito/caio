from typing import Optional, Union, Callable, Any


# noinspection PyPropertyDefinition
class Context:
    def __init__(self, max_requests: int = 32): ...

    @property
    def fileno(self) -> int: ...

    def submit(self, *aio_operations) -> int: ...

    def read(self, *aio_operations) -> int: ...

    def poll(self) -> int: ...

    def process_events(self, max_events: int = 512, min_events: int = 0,
                       timeout: int = 0) -> int: ...


# noinspection PyPropertyDefinition
class Operation:
    @classmethod
    def read(cls, nbytes: int, fd: int,
             offset: int, priority=0) -> Operation:
        """
        Creates a new instance of AIOOperation on read mode.
        """

    @classmethod
    def write(cls, payload_bytes: bytes, fd: int,
              offset: int, priority=0) -> Operation:
        """
        Creates a new instance of AIOOperation on write mode.
        """

    @classmethod
    def fsync(cls, fd: int, priority=0) -> Operation:

        """
        Creates a new instance of AIOOperation on fsync mode.
        """

    @classmethod
    def fdsync(cls, fd: int, priority=0) -> Operation:

        """
        Creates a new instance of AIOOperation on fdsync mode.
        """

    def get_value(self) -> Optional[bytes]:
        """
        Method returns a bytes value of AIOOperation's result or None.
        """

    @property
    def context(self) -> Context: ...

    @property
    def fileno(self) -> int: ...

    @property
    def priority(self) -> int: ...

    @property
    def offset(self) -> int: ...

    @property
    def payload(self) -> Optional[Union[bytes, memoryview]]: ...

    @property
    def nbytes(self) -> int: ...

    def set_callback(self, callback : Callable[[int], Any]) -> bool: ...

