"""Fixtures that state their return type, in the shapes the conversion recovers a type from.

The only corpus suite whose fixtures are annotated, and so the only one that exercises the
recovery half of the type inference rather than its fallback. Contributes the plain `-> X` case
(`session`), the generator case whose annotation names what it yields (`widget`), the aliased
typing spelling that is not one of the shapes to unwrap (`catalogue`, through `import typing as
t`), the async-generator case (`channel`), and the two shapes there is nothing to recover from: a
factory with no annotation at all (`untyped`) and one annotated `-> Any` (`opaque`), which is the
same amount of information said out loud.

Annotations here are objects, evaluated when each `def` is read. `store/conftest.py` is the same
suite's other half, under `from __future__ import annotations`, where they are strings.
"""

import typing as t
from collections.abc import Iterator
from typing import Any

import pytest

from support import Session, Widget


@pytest.fixture
def session() -> Session:
    return Session("sqlite:///showcase")


@pytest.fixture
def widget() -> Iterator[Widget]:
    yield Widget("spindle")


@pytest.fixture
def catalogue() -> t.Mapping[str, Widget]:
    return {"spindle": Widget("spindle")}


@pytest.fixture
async def channel() -> t.AsyncIterator[list[str]]:
    messages: list[str] = []
    yield messages
    messages.clear()


@pytest.fixture
def untyped():
    return {"nothing": "says what this is"}


@pytest.fixture
def opaque() -> Any:
    return object()
