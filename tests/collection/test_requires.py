"""Tests for velox._collection.requires: `velox.use(...)` and how collection reads it back."""

from __future__ import annotations

import sys
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


def test_a_malformed_declared_graph_is_one_error_for_the_file(tmp_path: Path) -> None:
    """A session-scoped declared fixture depending on a function-scoped one is the same `DIError`
    a `Depends()` site would raise. The declared graph is shared by the whole module, so it is
    reported once rather than once per test it applies to."""
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

async def test_two():
    pass

async def test_three():
    pass
""",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert len(result.errors) == 1
    assert "wide" in result.errors[0].message


def test_a_declaration_on_a_module_holding_no_tests_is_reported(tmp_path: Path) -> None:
    """A `velox.use(...)` in a shared helper module is never read back, so the fixtures it names
    would silently never run."""
    _write(
        tmp_path / "shared_helpers.py",
        """
import velox

@velox.fixture()
def declared():
    return 1

velox.use(declared)
""",
    )
    path = _write(
        tmp_path / "test_sample.py",
        f"""
import sys
sys.path.insert(0, {str(tmp_path)!r})
import shared_helpers

async def test_one():
    pass
""",
    )

    try:
        result = collect([path], rootdir=tmp_path)
    finally:
        sys.modules.pop("shared_helpers", None)
        sys.path.remove(str(tmp_path))

    assert [record.qualname for record in result.records] == ["test_one"]
    assert len(result.errors) == 1
    assert "shared_helpers" in result.errors[0].message


# Packages: a declaration on an `__init__.py`, reaching that directory and below.
# ------------------------------------------------------------------------------------------


_DECLARING_PACKAGE = """
import velox

@velox.fixture()
def from_package():
    return "from_package"

velox.use(from_package)
"""

_PLAIN_TEST = "async def test_one():\n    pass\n"


def test_a_package_declaration_reaches_a_test_beside_it(tmp_path: Path) -> None:
    _write(tmp_path / "pkg" / "__init__.py", _DECLARING_PACKAGE)
    path = _write(tmp_path / "pkg" / "test_sample.py", _PLAIN_TEST)

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert _step_names(result.records[0]) == ["from_package"]


def test_a_package_declaration_reaches_a_test_further_down(tmp_path: Path) -> None:
    _write(tmp_path / "pkg" / "__init__.py", _DECLARING_PACKAGE)
    _write(tmp_path / "pkg" / "deep" / "__init__.py", "")
    path = _write(tmp_path / "pkg" / "deep" / "test_sample.py", _PLAIN_TEST)

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert _step_names(result.records[0]) == ["from_package"]


def test_declarations_apply_outermost_first(tmp_path: Path) -> None:
    """Outer package, then inner package, then the module — the order they are torn down in
    reverse."""
    _write(tmp_path / "pkg" / "__init__.py", _DECLARING_PACKAGE)
    _write(
        tmp_path / "pkg" / "deep" / "__init__.py",
        """
import velox

@velox.fixture()
def from_subpackage():
    return "from_subpackage"

velox.use(from_subpackage)
""",
    )
    path = _write(tmp_path / "pkg" / "deep" / "test_sample.py", _DECLARING_MODULE)

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert _step_names(result.records[0]) == ["from_package", "from_subpackage", "declared"]


def test_the_walk_stops_at_a_directory_without_an_init(tmp_path: Path) -> None:
    """The package chain is what an import would traverse, so a gap ends it."""
    _write(tmp_path / "pkg" / "__init__.py", _DECLARING_PACKAGE)
    path = _write(tmp_path / "pkg" / "loose" / "test_sample.py", _PLAIN_TEST)

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert _step_names(result.records[0]) == []


def test_the_walk_stops_at_rootdir(tmp_path: Path) -> None:
    _write(tmp_path / "__init__.py", _DECLARING_PACKAGE)
    _write(tmp_path / "proj" / "__init__.py", "")
    path = _write(tmp_path / "proj" / "test_sample.py", _PLAIN_TEST)

    result = collect([path], rootdir=tmp_path / "proj")

    assert result.errors == []
    assert _step_names(result.records[0]) == []


def test_every_test_under_a_package_shares_one_fixture_object(tmp_path: Path) -> None:
    """One import per package per run: two would be two identities, and a session-scoped declared
    fixture would build once per subtree that imported it."""
    _write(tmp_path / "pkg" / "__init__.py", _DECLARING_PACKAGE)
    _write(tmp_path / "pkg" / "one" / "__init__.py", "")
    _write(tmp_path / "pkg" / "two" / "__init__.py", "")
    first = _write(tmp_path / "pkg" / "one" / "test_first.py", _PLAIN_TEST)
    second = _write(tmp_path / "pkg" / "two" / "test_second.py", _PLAIN_TEST)

    result = collect([first, second], rootdir=tmp_path)

    (one, two) = result.records
    assert one.plan.steps[0].fixture is two.plan.steps[0].fixture


def test_a_fixture_declared_by_both_a_package_and_a_module_gets_one_step(tmp_path: Path) -> None:
    """`scope="call"` has no single-flight cache to fold a repeat back together, so a duplicate
    declaration would otherwise build it twice."""
    _write(
        tmp_path / "declarations.py",
        """
import velox

@velox.fixture(scope="call")
def shared():
    return 1
""",
    )
    header = f"""
import sys

sys.path.insert(0, {str(tmp_path)!r})

import velox
from declarations import shared

velox.use(shared)
"""
    _write(tmp_path / "pkg" / "__init__.py", header)
    path = _write(
        tmp_path / "pkg" / "test_sample.py", header + "\nasync def test_one():\n    pass\n"
    )

    try:
        result = collect([path], rootdir=tmp_path)
    finally:
        sys.modules.pop("declarations", None)
        while str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))

    assert result.errors == []
    assert _step_names(result.records[0]) == ["shared"]


def test_a_package_that_fails_to_import_is_one_error_and_no_records(tmp_path: Path) -> None:
    """Its tests are skipped rather than run without the fixtures it declares — and the failure
    is reported once, not once per file underneath it."""
    _write(tmp_path / "pkg" / "__init__.py", "raise RuntimeError('boom')\n")
    first = _write(tmp_path / "pkg" / "test_first.py", _PLAIN_TEST)
    second = _write(tmp_path / "pkg" / "test_second.py", _PLAIN_TEST)

    result = collect([first, second], rootdir=tmp_path)

    assert result.records == []
    assert len(result.errors) == 1
    assert "boom" in result.errors[0].message
    assert result.errors[0].path == Path("pkg/__init__.py")


def test_a_package_is_not_left_in_sys_modules(tmp_path: Path) -> None:
    _write(tmp_path / "pkg" / "__init__.py", _DECLARING_PACKAGE)
    path = _write(tmp_path / "pkg" / "test_sample.py", _PLAIN_TEST)

    collect([path], rootdir=tmp_path)

    assert "velox_tests.pkg.__init__" not in sys.modules


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
