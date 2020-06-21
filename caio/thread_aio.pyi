from .abstract import AbstractContext, AbstractOperation


# noinspection PyPropertyDefinition
class Context(AbstractContext):
    def __init__(self, max_requests: int = 32, pool_size=8): ...


# noinspection PyPropertyDefinition
class Operation(AbstractOperation): ...
