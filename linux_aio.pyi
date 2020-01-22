from typing import Optional, Union


# noinspection PyPropertyDefinition
class AIOContext:
    def __init__(self, max_requests: int = 32): ...

    def submit(self, *aio_operations) -> int: ...


class EventFD:
    def __init__(self): ...

    # noinspection PyPropertyDefinition
    @property
    def fileno(self) -> int: ...

    def read(self) -> int: ...


# noinspection PyPropertyDefinition
class AIOOperation:
    @classmethod
    def read(cls, nbytes: int, fd: int,
             offset: int, priority=0) -> AIOOperation:
        """
        Creates a new instance of AIOOperation on read mode.
        """

    @classmethod
    def write(cls, payload_bytes: bytes, fd: int,
              offset: int, priority=0) -> AIOOperation:
        """
        Creates a new instance of AIOOperation on write mode.
        """

    @classmethod
    def fsync(cls, fd: int, priority=0) -> AIOOperation:

        """
        Creates a new instance of AIOOperation on fsync mode.
        """

    @classmethod
    def fdsync(cls, fd: int, priority=0) -> AIOOperation:

        """
        Creates a new instance of AIOOperation on fdsync mode.
        """

    def get_value(self) -> Optional[bytes]:
        """
        Method returns a bytes value of AIOOperation's result or None.
        """

    def submit(self, context: AIOContext, eventfd: EventFD) -> int:
        """
        Submit operation to kernel space.
        """

    def prepare(self, eventfd: EventFD) -> int:
        """
        Prepare operation for batch submit thought AIOContext.
        """

    @property
    def eventfd(self) -> EventFD: ...

    @property
    def context(self) -> AIOContext: ...

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
