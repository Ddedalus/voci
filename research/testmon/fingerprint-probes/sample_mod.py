"""Sample module exercising a wide variety of code-object-producing constructs."""
from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import overload


def plain_function(x):
    return x + 1


def outer():
    def inner():
        return 1

    return inner


def closure_maker(n):
    def adder(x):
        return x + n

    return adder


make_lambda = lambda x: x * 2  # noqa: E731


def has_comprehensions():
    listcomp = [i for i in range(3)]
    setcomp = {i for i in range(3)}
    dictcomp = {i: i for i in range(3)}
    genexpr = (i for i in range(3))
    return listcomp, setcomp, dictcomp, list(genexpr)


def generator_function():
    yield 1
    yield 2


async def async_function():
    return 1


async def async_generator():
    yield 1


def plain_decorator(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    return wrapper


@plain_decorator
def decorated_function(x):
    return x


def _external_wrapper(*args, **kwargs):
    return "external"


def decorator_returning_elsewhere(func):
    return _external_wrapper


@decorator_returning_elsewhere
def decorated_elsewhere():
    return "original"


class Outer:
    class_attr = 1

    def method(self):
        return self.class_attr

    def method_with_nested(self):
        def nested():
            return 1

        return nested()

    @property
    def prop(self):
        return self.class_attr

    @classmethod
    def cls_method(cls):
        return cls.class_attr

    @staticmethod
    def static_method():
        return 1

    class Inner:
        def inner_method(self):
            return 1

        class InnerInner:
            def deepest(self):
                return 1


@dataclass
class Point:
    x: int
    y: int


def make_partial():
    return functools.partial(plain_function, 1)


@overload
def overloaded(x: int) -> int: ...
@overload
def overloaded(x: str) -> str: ...
def overloaded(x):
    return x


def uses_match(x):
    match x:
        case 0:
            return "zero"
        case [a, b]:
            return a + b
        case _:
            return "other"


def generic_function[T](x: T) -> T:
    return x


class GenericClass[T]:
    def method(self, x: T) -> T:
        return x


def has_annotation(x: int, y: "SomeType" = None) -> "SomeType":  # noqa: F821
    return x
