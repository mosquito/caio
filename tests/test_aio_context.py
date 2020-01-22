import pytest
from linux_aio import AIOContext


def test_aio_context():
    ctx = AIOContext()
    assert ctx is not None

    ctx = AIOContext(1)
    assert ctx is not None

    ctx = AIOContext(32218)
    assert ctx is not None

    with pytest.raises(SystemError):
        AIOContext(65534)
