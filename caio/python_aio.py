import operator
import os
import sys
import threading
from collections.abc import Callable
from enum import IntEnum, unique
from multiprocessing.pool import ThreadPool
from threading import Lock, RLock
from types import MappingProxyType
from typing import Any
from weakref import WeakValueDictionary

from .abstract import AbstractContext, AbstractOperation

fdsync = getattr(os, "fdatasync", os.fsync)
NATIVE_PREAD_PWRITE = hasattr(os, "pread") and hasattr(os, "pwrite")


@unique
class OpCode(IntEnum):
    READ = 0
    WRITE = 1
    FSYNC = 2
    FDSYNC = 3
    NOOP = -1


@unique
class ContextState(IntEnum):
    OPEN = 0
    CLOSING = 1
    CLOSED = 2


class Context(AbstractContext):
    """
    python aio context implementation
    """

    MAX_POOL_SIZE = 128

    def __init__(self, max_requests: int = 32, pool_size: int = 8):
        # Set before any validation that can raise: __del__ runs even on a
        # partially-constructed object, and close() below relies on _state
        # existing and being non-OPEN to safely no-op before ever touching
        # _lock/pool, which aren't created until validation passes.
        self._state = ContextState.CLOSED

        if not (0 < pool_size < self.MAX_POOL_SIZE):
            raise ValueError(
                f"pool_size must be between 1 and {self.MAX_POOL_SIZE - 1}, "
                f"got {pool_size}",
            )
        if max_requests <= 0:
            raise ValueError(
                f"max_requests must be a positive integer, got {max_requests}",
            )

        self.__max_requests = max_requests
        self.pool = ThreadPool(pool_size)
        self._in_progress = 0
        self._lock = Lock()
        self._state = ContextState.OPEN

        if not NATIVE_PREAD_PWRITE:
            self._locks_cleaner = Lock()
            self._locks: WeakValueDictionary = WeakValueDictionary()

    @property
    def max_requests(self) -> int:
        return self.__max_requests

    @staticmethod
    def _invoke_callback(operation: "Operation", value):
        """
        Calls ``operation``'s callback with ``value``, if one was set via
        ``set_callback()``. A missing callback is a normal, expected case.
        A raising callback must not escape - this runs on ThreadPool's
        single internal result-handler thread, and an uncaught exception
        there kills that thread, silently stalling every future
        result/callback for the rest of this Context's lifetime.
        """
        with operation._lock:
            callback = operation.callback
        if callback is None:
            return

        try:
            callback(value)
        except BaseException:  # noqa: BLE001 (must isolate any callback exception, including SystemExit/KeyboardInterrupt)
            threading.excepthook(
                threading.ExceptHookArgs(
                    (*sys.exc_info(), threading.current_thread()),
                ),
            )

    def _release_slot(self):
        with self._lock:
            self._in_progress -= 1

    def _rollback_claim(self, operation: "Operation"):
        """
        Undoes a claim that was never actually scheduled (e.g. a
        concurrent close() tore down the pool between the capacity
        reservation and apply_async()) - unlike genuine completion, this
        must reset operation.in_progress, since the operation never
        actually ran and must stay retryable.
        """
        with operation._lock, self._lock:
            operation.in_progress = False
            self._in_progress -= 1

    def _execute(self, operation: "Operation") -> bool:
        """
        Returns True if actually scheduled, False if skipped because
        ``operation`` was already in progress - matches the other three
        backends, which all silently skip (rather than raise on, or
        dispatch twice) a resubmit of an Operation still in flight.
        """
        handler = self._OP_MAP[operation.opcode]

        # operation.in_progress is deliberately NOT reset on genuine
        # completion below - one-shot forever once actually scheduled,
        # matching all three native backends: a completed Operation must
        # not be resubmittable, only a fresh one constructed for a retry.
        def on_error(exc):
            self._release_slot()
            operation.exception = exc
            operation.written = 0
            self._invoke_callback(operation, None)

        def on_success(result):
            self._release_slot()
            operation.written = result
            self._invoke_callback(operation, result)

        # operation.in_progress is the Operation's own lock, not this
        # Context's - two different Contexts submitting the same Operation
        # only ever share the Operation, never a Context, so a per-Context
        # lock alone can't stop them both from claiming it.
        with operation._lock, self._lock:
            if operation.in_progress:
                return False

            if self._state != ContextState.OPEN:
                raise RuntimeError("Context is closed")

            if self._in_progress >= self.__max_requests:
                raise RuntimeError(
                    "Maximum simultaneous requests have been reached",
                )

            operation.in_progress = True
            self._in_progress += 1

        try:
            self.pool.apply_async(
                handler, args=(self, operation),
                callback=on_success,
                error_callback=on_error,
            )
        except BaseException:
            # Scheduling itself failed (e.g. a concurrent close() already
            # tore down the pool) - the slot reserved above was never
            # actually claimed by a real job, so it must be given back
            # instead of permanently inflating _in_progress.
            self._rollback_claim(operation)
            raise

        return True

    if NATIVE_PREAD_PWRITE:
        def __pread(self, fd, size, offset):
            return os.pread(fd, size, offset)

        def __pwrite(self, fd, bytes, offset):
            return os.pwrite(fd, bytes, offset)
    else:
        def __fd_lock(self, fd):
            # Plain get-or-create under _locks_cleaner - two threads racing
            # on the same fd's first access must get back the same RLock,
            # or their lseek()+read()/write() pairs can interleave. The
            # WeakValueDictionary itself only keeps an fd's entry alive
            # while some __pread()/__pwrite() call still holds a strong ref
            # to it (the `with` block below) - otherwise this Context would
            # accumulate one RLock per fd it's ever touched, forever.
            with self._locks_cleaner:
                lock = self._locks.get(fd)
                if lock is None:
                    lock = self._locks[fd] = RLock()
                return lock

        def __pread(self, fd, size, offset):
            with self.__fd_lock(fd):
                os.lseek(fd, 0, os.SEEK_SET)
                os.lseek(fd, offset, os.SEEK_SET)
                return os.read(fd, size)

        def __pwrite(self, fd, bytes, offset):
            with self.__fd_lock(fd):
                os.lseek(fd, 0, os.SEEK_SET)
                os.lseek(fd, offset, os.SEEK_SET)
                return os.write(fd, bytes)

    def _handle_read(self, operation: "Operation"):
        # Stored directly, not copied through a BytesIO - pread() already
        # returns exactly the bytes object get_value()/payload need to hand
        # back, and buffering it through BytesIO.write() just to unwrap it
        # again later cost a full extra copy per read for no benefit.
        data = self.__pread(
            operation.fileno, operation.nbytes, operation.offset,
        )
        operation.buffer = data
        return len(data)

    def _handle_write(self, operation: "Operation"):
        # operation.buffer is the caller's own payload bytes, unwrapped -
        # no BytesIO round-trip needed to hand it to pwrite() either.
        return self.__pwrite(
            operation.fileno, operation.buffer, operation.offset,
        )

    def _handle_fsync(self, operation: "Operation"):
        return os.fsync(operation.fileno)

    def _handle_fdsync(self, operation: "Operation"):
        return fdsync(operation.fileno)

    def _handle_noop(self, operation: "Operation"):
        return

    def submit(self, *aio_operations) -> int:
        for operation in aio_operations:
            if not isinstance(operation, Operation):
                raise ValueError(f"Invalid Operation {operation!r}")  # noqa: TRY004 (pre-existing public exception type, not changing it here)

        count = 0
        for operation in aio_operations:
            if self._execute(operation):
                count += 1

        return count

    def cancel(self, *aio_operations) -> int:
        """
        Cancels multiple Operations. Returns

         Operation.cancel(aio_op1, aio_op2, aio_opN, ...) -> int

        (Always returns zero, this method exists for compatibility reasons)
        """
        return 0

    def close(self):
        if self._state != ContextState.OPEN:
            return
        with self._lock:
            if self._state != ContextState.OPEN:
                return
            self._state = ContextState.CLOSING
            self.pool.close()
            self._state = ContextState.CLOSED

    def __del__(self):
        self.close()

    _OP_MAP = MappingProxyType({
        OpCode.READ: _handle_read,
        OpCode.WRITE: _handle_write,
        OpCode.FSYNC: _handle_fsync,
        OpCode.FDSYNC: _handle_fdsync,
        OpCode.NOOP: _handle_noop,
    })


# noinspection PyPropertyDefinition
class Operation(AbstractOperation):
    """
    python aio operation implementation
    """
    def __init__(
        self,
        fd: int,
        nbytes: int | None,
        offset: int | None,
        opcode: OpCode,
        payload: bytes | None = None,
        priority: int | None = None,
    ):
        # Validated eagerly, at construction time - matching the other 3
        # backends, which reject a non-int-like fd/nbytes/offset/priority
        # (or a non-bytes write payload) synchronously via
        # PyArg_ParseTupleAndKeywords/PyBytes_Check, rather than storing it
        # and failing later inside a worker thread. operator.index() is the
        # same __index__-based coercion PyArg_ParseTupleAndKeywords' "I"/"K"
        # format codes use internally, so this accepts exactly what the C
        # constructors accept (plain ints, numpy-style int-likes, ...) and
        # rejects exactly what they reject (str, float, ...).
        fd = operator.index(fd)
        if nbytes is not None:
            nbytes = operator.index(nbytes)
        if offset is not None:
            offset = operator.index(offset)
        if priority is not None:
            priority = operator.index(priority)

        # Plain bytes, not a BytesIO wrapper - for a write this is the
        # caller's own payload, handed to pwrite() as-is; for a read it
        # starts empty and _handle_read() replaces it with pread()'s
        # result directly. Either way there's nothing to unwrap later, so
        # get_value()/payload return it with no extra copy - and it's
        # never None, so callers don't need to check.
        if opcode == OpCode.WRITE:
            if not isinstance(payload, bytes):
                raise ValueError(f"payload_bytes must be bytes, got {payload!r}")
            buffer = payload
        else:
            buffer = b""

        self.callback: Callable[[int], Any] | None = None
        self.in_progress = False
        self._lock = Lock()
        self.buffer: bytes = buffer

        self.opcode = opcode
        self.__fileno = fd
        self.__offset = offset or 0
        self.__opcode = opcode
        self.__nbytes = nbytes or 0
        self.__priority = priority or 0
        self.exception = None
        self.written = 0

    @classmethod
    def read(
        cls, nbytes: int, fd: int, offset: int, priority=0,
    ) -> "Operation":
        """
        Creates a new instance of Operation on read mode.
        """
        return cls(fd, nbytes, offset, opcode=OpCode.READ, priority=priority)

    @classmethod
    def write(
        cls, payload_bytes: bytes, fd: int, offset: int, priority=0,
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

    def get_value(self) -> bytes | int | None:
        """
        Method returns a bytes value of AIOOperation's result or None.
        """
        if self.exception:
            raise self.exception

        if self.opcode == OpCode.WRITE:
            return self.written

        if self.opcode in (OpCode.FSYNC, OpCode.FDSYNC):
            return None

        return self.buffer

    @property
    def fileno(self) -> int:
        return self.__fileno

    @property
    def offset(self) -> int:
        return self.__offset

    @property
    def payload(self) -> memoryview | None:
        return memoryview(self.buffer)

    @property
    def nbytes(self) -> int:
        return self.__nbytes

    def set_callback(self, callback: Callable[[int], Any]) -> bool:
        if not callable(callback):
            raise ValueError(f"callback must be callable, got {callback!r}")  # noqa: TRY004 (pre-existing public exception type, not changing it here)
        with self._lock:
            self.callback = callback
        return True
