"""Decorator-populated registry, consumed by name through REGISTRY[...]()."""
REGISTRY = {}


def register(name):
    def decorator(fn):
        REGISTRY[name] = fn
        return fn
    return decorator


@register("greet")
def greet_handler():
    return "hello"


@register("farewell")
def farewell_handler():
    return "bye"


def call_registered(name):
    return REGISTRY[name]()
