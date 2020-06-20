
def test_aio_context(context_maker):
    ctx = context_maker()
    assert ctx is not None

    ctx = context_maker(1)
    assert ctx is not None

    ctx = context_maker(32218)
    assert ctx is not None
