from .abstract import AbstractContext, AbstractOperation


# noinspection PyPropertyDefinition
class Context(AbstractContext):
    def __init__(self, max_requests: int = 32): ...


# noinspection PyPropertyDefinition
class Operation(AbstractOperation): ...
