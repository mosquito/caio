import abc
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AbstractContext(Protocol):
    """
    Structural interface (`typing.Protocol`, not a plain `abc.ABC`)
    deliberately: the three native backends (`thread_aio`/`linux_aio`/
    `linux_uring`) are PyO3 classes generated straight from Rust via
    pyo3-stub-gen (see each backend's own `.pyi`) - they satisfy this
    interface by having the right methods/properties, not by literally
    declaring `class Context(AbstractContext):` in a hand-maintained
    stub. Protocol conformance is checked structurally by mypy, so no
    such declaration is needed. `python_aio.Context` still explicitly
    subclasses this below - that continues to work exactly as before,
    Protocol classes support being subclassed like an ordinary ABC too.
    """

    @property
    def max_requests(self) -> int:
        raise NotImplementedError

    def submit(self, *aio_operations) -> int:
        raise NotImplementedError(aio_operations)

    def cancel(self, *aio_operations) -> int:
        raise NotImplementedError(aio_operations)


@runtime_checkable
class AbstractOperation(Protocol):
    """
    fd lifetime contract: `fd` is stored as a plain integer and only
    dereferenced later, once the operation actually runs - deferred to a
    worker thread for thread_aio, to a later flush()/io_uring_enter() call
    for linux_uring, immediately (synchronously, within submit() itself)
    for linux_aio and python_aio. The caller must keep `fd` open and
    pointed at the same file until the operation completes (or is
    cancelled); closing it early and letting something else reuse that
    same fd number is undefined behavior here, same as it would be for any
    other in-flight async I/O against a raw fd (including the kernel's own
    io_uring, absent its separate registered-files mechanism). caio does
    not duplicate/pin the descriptor on the caller's behalf - deliberately,
    since doing so would cost an extra dup()/close() syscall pair per
    operation, defeating a large part of the point of using io_uring in
    particular.
    """

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
    def get_value(self) -> bytes | int:
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
