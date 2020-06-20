import os
from enum import IntEnum, unique
from io import BytesIO
from queue import Queue
from typing import Optional, Callable, Any
from threading import Thread


fdsync = getattr(os, "fdatasync", os.fsync)


class Context:
    def __init__(self, max_requests: int = 32, pool_size: int = 8):
        assert max_requests < 65535 or max_requests is None
        assert pool_size < 128

        self.queue = Queue(max_requests)
        self.pool = set()
        for _ in range(pool_size):
            thread = Thread(target=self._in_thread)
            thread.daemon = True
            self.pool.add(thread)
            thread.start()

    @staticmethod
    def _handle_read(operation: "Operation"):
        return operation.buffer.write(
            os.pread(operation.fileno, operation.nbytes, operation.offset)
        )

    @staticmethod
    def _handle_write(operation: "Operation"):
        return os.pwrite(
            operation.fileno, operation.buffer.getvalue(), operation.offset
        )

    @staticmethod
    def _handle_fsync(operation: "Operation"):
        return os.fsync(operation.fileno)

    @staticmethod
    def _handle_fdsync(operation: "Operation"):
        return fdsync(operation.fileno)

    @staticmethod
    def _handle_noop(operation: "Operation"):
        return

    def _in_thread(self):
        op_map = {
            OpCode.READ: self._handle_read,
            OpCode.WRITE: self._handle_write,
            OpCode.FSYNC: self._handle_fsync,
            OpCode.FDSYNC: self._handle_fdsync,
            OpCode.NOOP: self._handle_noop,
        }
        while True:
            operation = self.queue.get()

            if operation is None:
                return

            result = 0
            try:
                result = op_map[operation.opcode](operation)
            except Exception as e:
                operation.exception = e

            if operation.callback is not None:
                operation.callback(result)

    @property
    def max_requests(self) -> int:
        return self.queue.maxsize

    def submit(self, *aio_operations) -> int:
        operations = []

        for operation in aio_operations:
            if not isinstance(operation, Operation):
                raise ValueError("Invalid Operation %r", operation)

            operations.append(operation)

        count = 0
        for operation in operations:
            self.queue.put_nowait(operation)
            count += 1

        return count

    def close(self):
        for _ in range(len(self.pool)):
            self.queue.put_nowait(None)
        self.pool.clear()

    def __del__(self):
        if self.pool:
            self.close()


@unique
class OpCode(IntEnum):
    READ = 0
    WRITE = 1
    FSYNC = 2
    FDSYNC = 3
    NOOP = -1


# noinspection PyPropertyDefinition
class Operation:
    def __init__(
        self,
        fd: int,
        nbytes: Optional[int],
        offset: Optional[int],
        opcode: OpCode,
        payload: bytes = None,
        priority: int = None,
    ):
        self.callback = None
        self.buffer = None

        if opcode == OpCode.READ:
            self.buffer = BytesIO()

        if opcode == OpCode.WRITE:
            self.buffer = BytesIO(payload)

        self.opcode = opcode
        self.__fileno = fd
        self.__offset = offset
        self.__opcode = opcode
        self.__nbytes = nbytes
        self.__priority = priority
        self.exception = None

    @classmethod
    def read(
        cls, nbytes: int, fd: int, offset: int, priority=0
    ) -> "Operation":
        """
        Creates a new instance of Operation on read mode.
        """
        return cls(fd, nbytes, offset, opcode=OpCode.READ, priority=priority)

    @classmethod
    def write(
        cls, payload_bytes: bytes, fd: int, offset: int, priority=0
    ) -> "Operation":
        """
        Creates a new instance of AIOOperation on write mode.
        """
        return cls(
            fd,
            len(payload_bytes),
            offset,
            payload=payload_bytes,
            opcode=OpCode.WRITE,
            priority=priority,
        )

    @classmethod
    def fsync(cls, fd: int, priority=0) -> "Operation":

        """
        Creates a new instance of AIOOperation on fsync mode.
        """
        return cls(fd, None, None, opcode=OpCode.FSYNC, priority=priority)

    @classmethod
    def fdsync(cls, fd: int, priority=0) -> "Operation":

        """
        Creates a new instance of AIOOperation on fdsync mode.
        """
        return cls(fd, None, None, opcode=OpCode.FDSYNC, priority=priority)

    def get_value(self) -> Optional[bytes]:
        """
        Method returns a bytes value of AIOOperation's result or None.
        """
        if self.exception:
            raise self.exception

        if self.opcode == OpCode.WRITE:
            return self.__nbytes

        if self.buffer is None:
            return
        return self.buffer.getvalue()

    @property
    def fileno(self) -> int:
        return self.__fileno

    @property
    def offset(self) -> int:
        return self.__offset

    @property
    def payload(self) -> Optional[memoryview]:
        return self.buffer.getbuffer()

    @property
    def nbytes(self) -> int:
        return self.__nbytes

    def set_callback(self, callback: Callable[[int], Any]) -> bool:
        self.callback = callback
        return True
