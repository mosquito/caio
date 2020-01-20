from typing import Optional, Union

from .context import AIOContext
from .eventfd import EventFD


# noinspection PyPropertyDefinition
class AIOOperation:
    @classmethod
    def read(cls,  nbytes: int, aio_context: AIOContext, eventfd: EventFD,
             fd: int, offset: int, priority=0) -> AIOOperation:
        """
        Creates a new instance of AIOOperation on read mode.
        """

    @classmethod
    def write(cls, payload_bytes: bytes, aio_context: AIOContext,
              eventfd: EventFD, fd: int, offset: int,
              priority=0) -> AIOOperation:
        """
        Creates a new instance of AIOOperation on write mode.
        """

    @classmethod
    def fsync(cls, aio_context: AIOContext, eventfd: EventFD,
              fd: int, priority=0) -> AIOOperation:

        """
        Creates a new instance of AIOOperation on fsync mode.
        """

    @classmethod
    def fdsync(cls, aio_context: AIOContext, eventfd: EventFD,
               fd: int, priority=0) -> AIOOperation:

        """
        Creates a new instance of AIOOperation on fdsync mode.
        """

    def get_value(self) -> Optional[bytes]:
        """
        Method returns a bytes value of AIOOperation's result or None.
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
