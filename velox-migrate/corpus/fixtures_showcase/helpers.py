"""A decorator that leaves no trail back to the function it wrapped.

Contributes the case where a fixture's recorded location is the wrapper's own, in a module that
is not the conftest the fixture belongs to.
"""


def opaque(fn):
    def inner(*args, **kwargs):
        return fn(*args, **kwargs)

    return inner
