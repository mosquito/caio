import pytest
from caio._aio import Context


def test_aio_context():
    ctx = Context()
    assert ctx is not None

    ctx = Context(1)
    assert ctx is not None

    ctx = Context(32218)
    assert ctx is not None

    with pytest.raises(SystemError):
        Context(65534)
