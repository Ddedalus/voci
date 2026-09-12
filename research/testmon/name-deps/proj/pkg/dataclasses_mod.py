"""Dataclass field default, Enum member, class attribute, base class,
__slots__."""
from dataclasses import dataclass
from enum import Enum


class Color(Enum):
    RED = "red"
    GREEN = "green"


class Base:
    kind = "base"


@dataclass
class Widget(Base):
    __slots__ = ("name", "color")
    name: str = "widget"
    color: Color = Color.RED


def make_widget() -> Widget:
    return Widget()
