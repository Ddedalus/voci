"""Tests for velox._collection.requires: `velox.use(...)` and how collection reads it back."""

from __future__ import annotations

from pathlib import Path

from _support import Project

import pytest
import velox
from velox._collection.collect import TestRecord as Record
from velox._collection.collect import collect
from velox._collection.requires import requires_of, use

_DECLARING_MODULE = """
import velox

@velox.fixture()
def declared():
    return "declared"

velox.use(declared)

async def test_one():
    pass

async def test_two():
    pass
"""


def _write(path: Path, source: str) -> Path:
    return Project(path.parent).write(path.name, source)


def _step_names(record: Record) -> list[str]:
    return [step.fixture.name for step in record.plan.steps]


# `use`: what the call itself accepts and where it may appear.
# ------------------------------------------------------------------------------------------


def test_use_rejects_something_that_is_not_a_fixture() -> None:
    with pytest.raises(TypeError, match="takes fixtures"):
        use("not a fixture")  # type: ignore[arg-type]


def test_use_rejects_an_undecorated_function() -> None:
    def looks_like_a_fixture() -> int:
        return 1

    with pytest.raises(TypeError, match=r"@velox\.fixture"):
        use(looks_like_a_fixture)  # type: ignore[arg-type]


def test_use_inside_a_function_is_rejected_rather_than_silently_ignored() -> None:
    """A call there would run when the test runs, long after collection built its plan."""

    @velox.fixture()
    def anything() -> int:
        return 1

    with pytest.raises(TypeError, match="module body"):
        use(anything)


def test_requires_of_an_object_with_no_declarations_is_empty() -> None:
    assert requires_of(object()) == ()


# Collection: the declared fixtures reaching every test in the module.
# ------------------------------------------------------------------------------------------


def test_a_declared_fixture_applies_to_every_test_in_the_module(tmp_path: Path) -> None:
    path = _write(tmp_path / "test_sample.py", _DECLARING_MODULE)

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.qualname for record in result.records] == ["test_one", "test_two"]
    assert all(_step_names(record) == ["declared"] for record in result.records)


def test_a_declared_fixture_binds_to_no_parameter(tmp_path: Path) -> None:
    """It is built for the test, but its value never reaches the test function."""
    path = _write(tmp_path / "test_sample.py", _DECLARING_MODULE)

    (record, _) = collect([path], rootdir=tmp_path).records

    assert record.plan.root_args == ()


def test_declarations_do_not_leak_between_modules(tmp_path: Path) -> None:
    declaring = _write(tmp_path / "test_declaring.py", _DECLARING_MODULE)
    plain = _write(tmp_path / "test_plain.py", "async def test_plain():\n    pass\n")

    result = collect([declaring, plain], rootdir=tmp_path)

    by_name = {record.qualname: record for record in result.records}
    assert _step_names(by_name["test_one"]) == ["declared"]
    assert _step_names(by_name["test_plain"]) == []


def test_repeated_calls_accumulate_in_source_order(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        """
import velox

@velox.fixture()
def first():
    return 1

@velox.fixture()
def second():
    return 2

velox.use(first)
velox.use(second)

async def test_one():
    pass
""",
    )

    (record,) = collect([path], rootdir=tmp_path).records

    assert _step_names(record) == ["first", "second"]


def test_a_declared_fixture_is_built_before_the_tests_own_dependency(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        """
import velox

@velox.fixture()
def declared():
    return "declared"

@velox.fixture()
def asked_for():
    return "asked_for"

velox.use(declared)

async def test_one(x=velox.Depends(asked_for)):
    pass
""",
    )

    (record,) = collect([path], rootdir=tmp_path).records

    assert _step_names(record) == ["declared", "asked_for"]


def test_a_malformed_declared_graph_becomes_a_collection_error(tmp_path: Path) -> None:
    """A session-scoped declared fixture depending on a function-scoped one is the same
    `DIError` a `Depends()` site would raise, attributed to each test it applies to."""
    path = _write(
        tmp_path / "test_sample.py",
        """
import velox

@velox.fixture(scope="function")
def narrow():
    return 1

@velox.fixture(scope="session")
def wide(x=velox.Depends(narrow)):
    return x

velox.use(wide)

async def test_one():
    pass
""",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert len(result.errors) == 1
    assert "wide" in result.errors[0].message


def test_a_declared_parametrized_fixture_fans_the_module_out_by_case(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        """
import velox

@velox.fixture(params=["sqlite", "postgres"])
def backend(param):
    return param

velox.use(backend)

async def test_one():
    pass
""",
    )

    result = collect([path], rootdir=tmp_path)

    assert [record.id.rsplit("::", 1)[1] for record in result.records] == [
        "test_one[sqlite]",
        "test_one[postgres]",
    ]
