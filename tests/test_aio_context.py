import tempfile

import pytest


def test_aio_context(context_maker):
    ctx = context_maker()
    assert ctx is not None

    ctx = context_maker(1)
    assert ctx is not None

    ctx = context_maker(32218)
    assert ctx is not None


def test_context_repr_does_not_crash(context_maker):
    ctx = context_maker()
    assert isinstance(repr(ctx), str)


def test_operation_repr_does_not_crash(backend):
    with tempfile.NamedTemporaryFile() as f:
        fd = f.fileno()
        ops = [
            backend.Operation.read(16, fd, 0),
            backend.Operation.write(b"hello", fd, 0),
            backend.Operation.fsync(fd),
            backend.Operation.fdsync(fd),
        ]
        for op in ops:
            assert isinstance(repr(op), str)


def test_write_operation_properties_before_submission(backend):
    """.fileno/.offset/.nbytes/.payload for a freshly constructed write
    Operation must reflect the constructor arguments identically across
    every backend, even before it's ever submitted - unlike read (see
    below), nothing here depends on backend-specific buffer allocation
    strategy."""
    with tempfile.NamedTemporaryFile() as f:
        fd = f.fileno()
        payload = b"the quick brown fox"
        op = backend.Operation.write(payload, fd, 7)

        assert op.fileno == fd
        assert op.offset == 7
        assert op.nbytes == len(payload)
        assert bytes(op.payload) == payload


def test_read_operation_nbytes_before_submission(backend):
    """Only .nbytes is guaranteed consistent for an unsubmitted read across
    backends - .payload's pre-submission content is an implementation
    detail (some backends pre-allocate a same-size buffer, python_aio
    starts with an empty BytesIO instead), so it's deliberately not
    asserted here."""
    with tempfile.NamedTemporaryFile() as f:
        fd = f.fileno()
        op = backend.Operation.read(16, fd, 3)

        assert op.fileno == fd
        assert op.offset == 3
        assert op.nbytes == 16


@pytest.mark.parametrize("ctor_name", ["fsync", "fdsync"])
def test_sync_operation_properties(backend, ctor_name):
    with tempfile.NamedTemporaryFile() as f:
        fd = f.fileno()
        op = getattr(backend.Operation, ctor_name)(fd)

        assert op.fileno == fd
        assert op.offset == 0
        assert op.nbytes == 0
        # Some backends return None, others an empty buffer - both falsy.
        assert not op.payload
