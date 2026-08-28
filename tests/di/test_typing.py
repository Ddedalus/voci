"""What a type checker sees at a `Depends(...)` site, asserted with `typing.assert_type`.

`assert_type` is a no-op at run time: `just checks typecheck` is what reads these assertions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import assert_type

from _support import run_async as run

import velox
from velox import (
    Capture,
    Depends,
    Fixture,
    LegacyPath,
    LegacyTmpPathFactory,
    LogRecords,
    TmpPathFactory,
)
from velox._di.fixtures import BuiltinContext
from velox._di.runtime import _construct

_DUMMY_CTX = BuiltinContext(test_id="dummy::test", module_path="dummy.py")


class Session:
    def query(self) -> int:
        return 1


@velox.fixture()
def plain() -> Session:
    return Session()


@velox.fixture()
def sync_generator() -> Iterator[Session]:
    yield Session()


@velox.fixture()
async def coroutine() -> Session:
    return Session()


@velox.fixture()
async def async_generator() -> AsyncIterator[Session]:
    yield Session()


@velox.fixture()
def iterator_valued() -> Iterator[Session]:
    """A fixture whose *value* is an iterator: it returns, it does not yield."""
    return iter([Session()])


# The four shapes `@velox.fixture()`'s overloads unwrap.
assert_type(plain, Fixture[Session])
assert_type(sync_generator, Fixture[Session])
assert_type(coroutine, Fixture[Session])
assert_type(async_generator, Fixture[Session])

# `iterator_valued` has the same declared signature as `sync_generator`, so the overloads read it
# as a generator fixture and unwrap a type it never yields. The run-time behaviour is the other
# one: `_di.runtime` dispatches on `inspect.isgeneratorfunction`, so the value is the iterator.
# docs/reference/fixtures.md carries the workaround.
assert_type(iterator_valued, Fixture[Session])

# Every built-in carries its own value type through to the injection site.
assert_type(Depends(velox.tmp_path), Path)
assert_type(Depends(velox.tmp_path_factory), TmpPathFactory)
assert_type(Depends(velox.capture), Capture)
assert_type(Depends(velox.log_records), LogRecords)
assert_type(Depends(velox.test_info), velox.TestInfo)
assert_type(Depends(velox.tmpdir), LegacyPath)
assert_type(Depends(velox.tmpdir_factory), LegacyTmpPathFactory)


def test_an_iterator_valued_fixture_hands_back_the_iterator() -> None:
    """Constructed through the runtime, so what is pinned is the value velox hands the parameter
    rather than what the function returns -- which is the half of the mismatch above that
    `assert_type` cannot see."""

    async def scenario() -> None:
        value, closer = await _construct(iterator_valued, {}, _DUMMY_CTX)
        assert isinstance(value, Iterator)
        assert closer is None

    run(scenario())
