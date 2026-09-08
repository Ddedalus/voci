"""Tests for voci._collection.collect: module import and collection errors."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, cast

import pytest
from _support import Project

from voci._assertions import rewrite as _rewrite
from voci._collection.collect import collect, module_name_for
from voci._collection.selection import compile_tag_expression
from voci._mocking import real_function


def _write(path: Path, source: str) -> Path:
    return Project(path.parent).write(path.name, source)


def test_module_name_for_is_path_derived_and_rootdir_relative(tmp_path: Path) -> None:
    path = tmp_path / "pkg" / "test_utils.py"
    assert module_name_for(path, tmp_path) == "voci_tests.pkg.test_utils"


def test_module_name_for_escapes_non_identifier_segments(tmp_path: Path) -> None:
    path = tmp_path / "api-v2" / "test_a.py"
    name = module_name_for(path, tmp_path)
    assert name.startswith("voci_tests.api_v2_")
    assert name.endswith(".test_a")


def test_module_name_for_two_same_named_files_never_collide(tmp_path: Path) -> None:
    first = module_name_for(tmp_path / "pkg_a" / "test_utils.py", tmp_path)
    second = module_name_for(tmp_path / "pkg_b" / "test_utils.py", tmp_path)
    assert first != second


def test_module_name_for_escaping_does_not_reopen_the_collision_it_guards_against(
    tmp_path: Path,
) -> None:
    """A bare character-replace (`-` -> `_`) would make `api-v2` and `api_v2` collide again;
    the digest appended for whichever segment needed escaping keeps them apart."""
    dashed = module_name_for(tmp_path / "api-v2" / "test_a.py", tmp_path)
    clean = module_name_for(tmp_path / "api_v2" / "test_a.py", tmp_path)
    assert dashed != clean


def test_collect_orders_records_by_definition_line_not_name(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "async def test_b():\n"
        "    pass\n"
        "\n"
        "def not_a_test():\n"
        "    pass\n"
        "\n"
        "async def test_a():\n"
        "    pass\n"
        "\n"
        "def test_sync():\n"  # sync `def test_*` — collected same as an async one
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.qualname for record in result.records] == ["test_b", "test_a", "test_sync"]
    assert [record.index for record in result.records] == [0, 1, 2]
    assert [record.lineno for record in result.records] == [1, 7, 10]
    # `path` is relative to `rootdir` -- not the absolute `tmp_path` the file actually lives
    # under, which would bake a machine-specific path into every id.
    assert result.records[0].id == "test_sample.py::test_b"
    assert result.records[0].path == Path("test_sample.py")


def test_sync_def_test_star_is_collected_on_its_own(tmp_path: Path) -> None:
    path = _write(tmp_path / "test_sample.py", "def test_sync():\n    pass\n")

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.qualname for record in result.records] == ["test_sync"]


def test_a_class_groups_its_test_methods_under_a_double_colon_id(tmp_path: Path) -> None:
    """`class Test*` is namespacing: its methods are collected, and the class name is a
    `::`-separated segment of the id, the way it reads in a pytest node id."""
    path = _write(
        tmp_path / "test_sample.py",
        "class TestSomething:\n"
        "    def test_sync(self):\n"
        "        pass\n"
        "\n"
        "    async def test_async(self):\n"
        "        pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.id for record in result.records] == [
        "test_sample.py::TestSomething::test_sync",
        "test_sample.py::TestSomething::test_async",
    ]
    assert [record.lineno for record in result.records] == [2, 5]


def test_a_class_test_runs_on_a_fresh_instance_per_test(tmp_path: Path) -> None:
    """Namespacing only: two tests of one class never see each other's `self`, which is what
    keeps them as independent as module-level tests under concurrent dispatch."""
    path = _write(
        tmp_path / "test_sample.py",
        "seen = []\n"
        "\n"
        "class TestSomething:\n"
        "    def test_one(self):\n"
        "        self.value = 1\n"
        "        seen.append(id(self))\n"
        "\n"
        "    def test_two(self):\n"
        "        assert not hasattr(self, 'value')\n"
        "        seen.append(id(self))\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    for record in result.records:
        record.func()
    # Through `real_function`: `record.func` is voci's own per-call wrapper, so the test
    # module's globals are on the method underneath it.
    seen = real_function(result.records[0].func).__globals__["seen"]
    assert len(set(seen)) == 2


def test_a_class_test_method_is_injected_like_any_other_test(tmp_path: Path) -> None:
    """`self` is supplied by voci, so it must not read as a parameter with no injection."""
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n"
        "from voci import Depends\n"
        "\n"
        "@voci.fixture()\n"
        "async def number() -> int:\n"
        "    return 7\n"
        "\n"
        "class TestSomething:\n"
        "    async def test_method(self, n: int = Depends(number)):\n"
        "        assert n == 7\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    (record,) = result.records
    assert record.plan.root_args


def test_static_and_class_methods_are_collected_too(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "class TestSomething:\n"
        "    @staticmethod\n"
        "    def test_static():\n"
        "        pass\n"
        "\n"
        "    @classmethod\n"
        "    def test_classmethod(cls):\n"
        "        assert cls.__name__ == 'TestSomething'\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.id for record in result.records] == [
        "test_sample.py::TestSomething::test_static",
        "test_sample.py::TestSomething::test_classmethod",
    ]
    for record in result.records:
        record.func()


def test_a_nested_class_extends_the_group_path(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "class TestOuter:\n"
        "    def test_outer(self):\n"
        "        pass\n"
        "\n"
        "    class TestInner:\n"
        "        def test_inner(self):\n"
        "            pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.id for record in result.records] == [
        "test_sample.py::TestOuter::test_outer",
        "test_sample.py::TestOuter::TestInner::test_inner",
    ]


def test_class_methods_and_module_functions_are_ordered_by_source_line(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "def test_first():\n"
        "    pass\n"
        "\n"
        "class TestGroup:\n"
        "    def test_second(self):\n"
        "        pass\n"
        "\n"
        "def test_third():\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert [record.id.split("::")[-1] for record in result.records] == [
        "test_first",
        "test_second",
        "test_third",
    ]


def test_a_parametrized_class_method_expands_per_case(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n"
        "\n"
        "class TestSomething:\n"
        "    @voci.parametrize('n', [1, 2])\n"
        "    def test_method(self, n):\n"
        "        assert n\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.id for record in result.records] == [
        "test_sample.py::TestSomething::test_method[1]",
        "test_sample.py::TestSomething::test_method[2]",
    ]


def test_a_group_collects_the_test_methods_it_inherits(tmp_path: Path) -> None:
    """The shared-base pattern: one set of tests, run once per backend that inherits them."""
    path = _write(
        tmp_path / "test_sample.py",
        "class SharedTests:\n"
        "    def test_shared(self):\n"
        "        pass\n"
        "\n"
        "class TestPostgres(SharedTests):\n"
        "    def test_own(self):\n"
        "        pass\n"
        "\n"
        "class TestSqlite(SharedTests):\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert sorted(record.id for record in result.records) == [
        "test_sample.py::TestPostgres::test_own",
        "test_sample.py::TestPostgres::test_shared",
        "test_sample.py::TestSqlite::test_shared",
    ]


def test_a_shared_base_of_tests_is_not_reported_as_a_misnamed_group(tmp_path: Path) -> None:
    """Its tests do run -- through every group that inherits them -- so there is nothing lost
    to report, even though the base itself is named like a suite."""
    path = _write(
        tmp_path / "test_sample.py",
        "class SharedTests:\n"
        "    def test_shared(self):\n"
        "        pass\n"
        "\n"
        "class TestPostgres(SharedTests):\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []


def test_an_overridden_test_method_is_collected_once_from_the_group(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "class SharedTests:\n"
        "    def test_shared(self):\n"
        "        raise AssertionError('base body must not run')\n"
        "\n"
        "class TestOverriding(SharedTests):\n"
        "    def test_shared(self):\n"
        "        pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    (record,) = result.records
    record.func()


def test_an_inherited_lifecycle_hook_is_a_collection_error(tmp_path: Path) -> None:
    """A hook on a base governs the group exactly as much as one written on it directly, and is
    just as invisible to the tests underneath."""
    path = _write(
        tmp_path / "test_sample.py",
        "class HookBase:\n"
        "    def setup_method(self):\n"
        "        self.client = object()\n"
        "\n"
        "class TestHooks(HookBase):\n"
        "    def test_method(self):\n"
        "        assert self.client\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert any("setup_method" in error.message for error in result.errors)


def test_an_inherited_init_is_a_collection_error(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "class Base:\n"
        "    def __init__(self, dependency):\n"
        "        self.dependency = dependency\n"
        "\n"
        "class TestSomething(Base):\n"
        "    def test_method(self):\n"
        "        pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert any("__init__" in error.message for error in result.errors)


def test_a_class_named_like_a_test_with_no_test_methods_is_not_flagged(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "class TestHelper:\n    def helper(self):\n        pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert result.records == []


def test_a_class_not_named_like_a_test_is_not_flagged_even_with_a_test_method(
    tmp_path: Path,
) -> None:
    """A helper class that happens to define a `test_*`-named method (a fake client with a
    `test_connection`, say) is ordinary code, not a suite voci lost."""
    path = _write(
        tmp_path / "test_sample.py",
        "class Helper:\n    def test_method(self):\n        pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert result.records == []


def test_a_class_with_an_init_is_a_collection_error(tmp_path: Path) -> None:
    """voci constructs the class itself, so an `__init__` it can't satisfy must be reported
    rather than left to fail once per test at run time."""
    path = _write(
        tmp_path / "test_sample.py",
        "class TestSomething:\n"
        "    def __init__(self, client):\n"
        "        self.client = client\n"
        "\n"
        "    def test_method(self):\n"
        "        pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    (error,) = result.errors
    assert error.path == Path("test_sample.py")
    assert "__init__" in error.message
    assert "test_method" in error.message


def test_lifecycle_hooks_on_a_class_are_a_collection_error(tmp_path: Path) -> None:
    """The dangerous shape: collecting these tests while silently never running their setup
    would report passes for tests that never got what they were written to expect."""
    path = _write(
        tmp_path / "test_sample.py",
        "class TestSomething:\n"
        "    def setup_method(self):\n"
        "        self.client = object()\n"
        "\n"
        "    def test_method(self):\n"
        "        assert self.client\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    (error,) = result.errors
    assert "setup_method" in error.message


def test_a_unittest_test_case_is_a_collection_error(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import unittest\n"
        "\n"
        "class TestSomething(unittest.TestCase):\n"
        "    def test_method(self):\n"
        "        self.assertTrue(True)\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    (error,) = result.errors
    assert "unittest" in error.message


def test_a_unittest_test_case_not_named_test_first_is_a_collection_error(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import unittest\n"
        "\n"
        "class SomethingTest(unittest.TestCase):\n"
        "    def test_method(self):\n"
        "        self.assertTrue(True)\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    (error,) = result.errors
    assert "unittest" in error.message


def test_a_mark_on_a_class_is_a_collection_error(tmp_path: Path) -> None:
    """A `@voci.skip` on the class would otherwise be read by nobody: its tests would run
    exactly as though the mark weren't there."""
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n"
        "\n"
        "@voci.skip('not yet')\n"
        "class TestSomething:\n"
        "    def test_method(self):\n"
        "        pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    (error,) = result.errors
    assert "mark" in error.message


def test_a_class_named_like_a_suite_but_not_test_first_is_a_collection_error(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "class UserTests:\n    def test_method(self):\n        pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    (error,) = result.errors
    assert "UserTests" in error.message
    assert "test_method" in error.message


def test_a_class_problem_does_not_stop_module_level_tests_in_the_same_file(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "class TestSomething:\n"
        "    def setup_method(self):\n"
        "        pass\n"
        "\n"
        "    def test_method(self):\n"
        "        pass\n"
        "\n"
        "def test_ok():\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert len(result.errors) == 1
    assert [record.qualname for record in result.records] == ["test_ok"]


def test_multiple_offending_classes_each_get_their_own_error(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "class TestA:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "\n"
        "    def test_a(self):\n"
        "        pass\n"
        "\n"
        "class TestB:\n"
        "    def setup_method(self):\n"
        "        pass\n"
        "\n"
        "    def test_b(self):\n"
        "        pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert {error.message.split(":", 1)[0] for error in result.errors} == {"TestA", "TestB"}


def test_a_test_name_bound_to_a_lambda_is_a_collection_error(tmp_path: Path) -> None:
    """`test_x = lambda: ...` collects as nothing at all: the object's own name is `<lambda>`,
    not `test_x`. Reported rather than silently absent."""
    path = _write(tmp_path / "test_sample.py", "test_x = lambda: None  # noqa: E731\n")

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    (error,) = result.errors
    assert "test_x" in error.message


def test_a_test_name_bound_to_a_function_defined_under_another_name_is_a_collection_error(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "def _implementation():\n    pass\n\ntest_thing = _implementation\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    (error,) = result.errors
    assert "test_thing" in error.message


def test_a_test_name_bound_to_a_callable_object_is_left_alone(tmp_path: Path) -> None:
    """`test_app = FastAPI()`, `test_client = Mock()`: callable, ordinary, and not a test body
    anyone meant voci to run."""
    path = _write(
        tmp_path / "test_sample.py",
        "class Client:\n    def __call__(self):\n        pass\n\ntest_client = Client()\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert result.records == []


def test_a_test_name_bound_to_data_is_left_alone(tmp_path: Path) -> None:
    """`test_cases = [...]` is data a test reads, not a test."""
    path = _write(tmp_path / "test_sample.py", "test_cases = [1, 2, 3]\n")

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert result.records == []


def test_a_test_function_imported_from_another_module_is_left_alone(tmp_path: Path) -> None:
    """Not collected here (it belongs to the module that defines it) and not reported either:
    the exclusion is deliberate, not a shape voci failed to understand."""
    _write(tmp_path / "helpers.py", "def test_shared():\n    pass\n")
    path = _write(
        tmp_path / "test_sample.py",
        f"import sys\nsys.path.insert(0, {str(tmp_path)!r})\nfrom helpers import test_shared\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert result.records == []


def test_a_generator_test_function_is_a_collection_error(tmp_path: Path) -> None:
    """Calling it returns a generator and runs none of its body -- a silent pass, which is
    exactly what a collection error exists to prevent."""
    path = _write(
        tmp_path / "test_sample.py",
        "def test_yielding():\n    yield 1\n    assert False\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    (error,) = result.errors
    assert "test_yielding" in error.message


def test_import_error_becomes_a_collection_error_and_does_not_abort(tmp_path: Path) -> None:
    broken = _write(tmp_path / "test_broken.py", "raise RuntimeError('boom')\n")
    fine = _write(
        tmp_path / "test_fine.py",
        "async def test_ok():\n    pass\n",
    )

    # The broken file is first: a failure must not stop collection of files after it.
    result = collect([broken, fine], rootdir=tmp_path)

    assert len(result.errors) == 1
    assert result.errors[0].path == Path("test_broken.py")
    assert "boom" in result.errors[0].message
    assert "RuntimeError" in result.errors[0].message

    assert [record.qualname for record in result.records] == ["test_ok"]
    assert result.records[0].index == 0


def test_a_spec_that_no_loader_can_be_built_for_becomes_a_collection_error(
    tmp_path: Path,
) -> None:
    """A path `spec_from_file_location` can't map to a loader (no rewrite hook installed, and
    no recognized suffix) surfaces as a `CollectionError`, not an uncaught `ImportError`."""
    path = _write(tmp_path / "test_sample.weird", "x = 1\n")

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert len(result.errors) == 1
    assert result.errors[0].path == Path("test_sample.weird")
    assert "cannot build an import spec" in result.errors[0].message


def test_index_is_assigned_once_across_the_full_concatenation(tmp_path: Path) -> None:
    first = _write(tmp_path / "test_first.py", "async def test_one():\n    pass\n")
    second = _write(tmp_path / "test_second.py", "async def test_two():\n    pass\n")

    result = collect([first, second], rootdir=tmp_path)

    assert [record.index for record in result.records] == [0, 1]
    assert [record.path for record in result.records] == [
        Path("test_first.py"),
        Path("test_second.py"),
    ]


def test_a_failed_import_does_not_leave_a_half_initialized_module_in_sys_modules(
    tmp_path: Path,
) -> None:
    broken = _write(tmp_path / "test_broken.py", "raise RuntimeError('boom')\n")

    result = collect([broken], rootdir=tmp_path)

    assert len(result.errors) == 1
    assert module_name_for(broken, tmp_path) not in sys.modules


def test_a_successful_import_is_also_not_left_resident_in_sys_modules(tmp_path: Path) -> None:
    """A successful import is also removed from `sys.modules` after collection."""
    fine = _write(tmp_path / "test_fine.py", "async def test_ok():\n    pass\n")

    result = collect([fine], rootdir=tmp_path)

    assert len(result.records) == 1
    assert module_name_for(fine, tmp_path) not in sys.modules


def test_helper_named_test_star_imported_from_elsewhere_is_not_collected_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `__module__` filter: a `test_*`-named function imported into a module from
    somewhere else must not be collected as if it were defined there."""
    # `test_main.py`'s `from ... import ...` below is an ordinary Python import statement, which
    # goes through the *real* import system (not voci's path-derived one) and therefore needs
    # the helper module findable on `sys.path` — voci itself never touches `sys.path`, this is
    # purely to make the test's own fixture module importable the normal way.
    monkeypatch.syspath_prepend(str(tmp_path))
    helper = _write(
        tmp_path / "voci_test_collect_helper.py", "async def test_helper():\n    pass\n"
    )
    main = _write(
        tmp_path / "test_main.py",
        "from voci_test_collect_helper import test_helper\n\nasync def test_own():\n    pass\n",
    )

    try:
        result = collect([helper, main], rootdir=tmp_path)
    finally:
        # The plain `import` statement above registers a real, un-prefixed `sys.modules` entry,
        # distinct from voci's own `voci_tests.voci_test_collect_helper`.
        sys.modules.pop("voci_test_collect_helper", None)

    ids = [record.id for record in result.records]
    assert ids == ["voci_test_collect_helper.py::test_helper", "test_main.py::test_own"]


def test_skip_marked_test_is_excluded_from_records_and_reported_skipped(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.skip('not ready')\n"
        "async def test_skipped():\n"
        "    raise AssertionError('must not run')\n\n"
        "async def test_runs():\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert [record.qualname for record in result.records] == ["test_runs"]
    assert len(result.skipped) == 1
    assert result.skipped[0].id == "test_sample.py::test_skipped"
    assert result.skipped[0].reason == "not ready"
    assert result.errors == []


def test_truthy_skipif_excludes_a_test_falsy_skipif_does_not(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.skipif(True, reason='always')\n"
        "async def test_always_skipped():\n"
        "    pass\n\n"
        "@voci.skipif(False, reason='never')\n"
        "async def test_not_skipped():\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert [record.qualname for record in result.records] == ["test_not_skipped"]
    assert [skipped.id for skipped in result.skipped] == ["test_sample.py::test_always_skipped"]


def test_tag_expr_excludes_non_matching_tests_into_deselected(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.tag('slow')\n"
        "async def test_slow():\n"
        "    pass\n\n"
        "async def test_untagged():\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path, tag_expr=compile_tag_expression("slow"))

    assert [record.qualname for record in result.records] == ["test_slow"]
    assert result.deselected == ["test_sample.py::test_untagged"]
    assert result.skipped == []
    assert result.errors == []


def test_skip_takes_priority_over_tag_expr_deselection(tmp_path: Path) -> None:
    """A skip-marked test is always `skipped`, never `deselected`, regardless of `-m` -- a
    test's skip status must not flip depending on which tags happen to be selected."""
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.tag('slow')\n"
        "@voci.skip('unrelated reason')\n"
        "async def test_slow_and_skipped():\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path, tag_expr=compile_tag_expression("not slow"))

    assert result.records == []
    assert result.deselected == []
    assert [skipped.id for skipped in result.skipped] == ["test_sample.py::test_slow_and_skipped"]
    assert result.skipped[0].reason == "unrelated reason"


def test_no_tag_expr_deselects_nothing(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n@voci.tag('slow')\nasync def test_it():\n    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert [record.qualname for record in result.records] == ["test_it"]
    assert result.deselected == []


def test_deselected_tests_do_not_consume_an_index(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "async def test_a():\n"
        "    pass\n\n"
        "@voci.tag('slow')\n"
        "async def test_b():\n"
        "    pass\n\n"
        "async def test_c():\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path, tag_expr=compile_tag_expression("not slow"))

    assert [record.qualname for record in result.records] == ["test_a", "test_c"]
    assert [record.index for record in result.records] == [0, 1]
    assert result.deselected == ["test_sample.py::test_b"]


def test_depends_defaulted_parameter_is_collected_with_a_real_resolution_plan(
    tmp_path: Path,
) -> None:
    """A well-formed `Depends(...)` graph is not refused — collection resolves it via
    `_fixtures.plan_for` and attaches the resulting `ResolutionPlan` to the `TestRecord`."""
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.fixture()\n"
        "async def db():\n"
        "    return 1\n\n"
        "async def test_needs_db(value: int = voci.Depends(db)):\n"
        "    assert value == 1\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert result.skipped == []
    assert len(result.records) == 1
    record = result.records[0]
    assert len(record.plan.steps) == 1
    assert record.plan.steps[0].fixture.name == "db"
    assert record.plan.root_args == (("value", 0, False),)


def test_a_test_with_no_dependencies_still_gets_a_trivial_resolution_plan(
    tmp_path: Path,
) -> None:
    """Every `TestRecord` carries a `plan`, even an empty one — `_run.py` has no "does this test
    have fixtures" branch to keep in sync."""
    path = _write(tmp_path / "test_sample.py", "async def test_plain():\n    pass\n")

    result = collect([path], rootdir=tmp_path)

    assert len(result.records) == 1
    assert result.records[0].plan.steps == ()
    assert result.records[0].plan.root_args == ()
    assert result.records[0].params is None


def test_a_parametrized_test_expands_into_one_record_per_case(tmp_path: Path) -> None:
    """The exact bug this closes: `n`/`expected` used to read as missing `Depends(...)`
    injections and fail collection outright."""
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.parametrize('n, expected', [(1, 2), (2, 4)])\n"
        "async def test_double(n, expected):\n"
        "    assert n * 2 == expected\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.id for record in result.records] == [
        "test_sample.py::test_double[1-2]",
        "test_sample.py::test_double[2-4]",
    ]
    assert [record.params for record in result.records] == [
        {"n": 1, "expected": 2},
        {"n": 2, "expected": 4},
    ]
    assert [record.index for record in result.records] == [0, 1]
    # Both cases share one function -> one resolution plan.
    assert result.records[0].plan is result.records[1].plan


def test_a_parametrized_test_shares_its_plan_with_an_actual_dependency(tmp_path: Path) -> None:
    """A parametrized argument and a `Depends(...)` injection on the same test coexist: the
    former becomes `params`, the latter is resolved through `plan` exactly as usual."""
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.fixture()\n"
        "async def db():\n"
        "    return 10\n\n"
        "@voci.parametrize('n', [1, 2])\n"
        "async def test_uses_both(n, value: int = voci.Depends(db)):\n"
        "    assert value == 10\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.params for record in result.records] == [{"n": 1}, {"n": 2}]
    for record in result.records:
        assert record.plan.steps[0].fixture.name == "db"


def test_stacked_parametrize_expands_the_full_cartesian_product(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.parametrize('outer', [1, 2])\n"
        "@voci.parametrize('inner', ['a', 'b'])\n"
        "async def test_grid(outer, inner):\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.id.rsplit("[", 1)[-1] for record in result.records] == [
        "1-a]",
        "1-b]",
        "2-a]",
        "2-b]",
    ]


def test_a_name_reused_across_stacked_parametrizes_is_a_collection_error(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.parametrize('n', [1, 2])\n"
        "@voci.parametrize('n', [3, 4])\n"
        "async def test_conflict(n):\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert len(result.errors) == 1
    assert "n" in result.errors[0].message


def test_a_parametrize_name_colliding_with_a_real_injection_is_a_collection_error(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.fixture()\n"
        "async def value():\n"
        "    return 1\n\n"
        "@voci.parametrize('value', [1, 2])\n"
        "async def test_conflict(value: int = voci.Depends(value)):\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert len(result.errors) == 1
    assert "value" in result.errors[0].message


def test_a_skipped_parametrized_test_is_reported_skipped_once_without_case_expansion(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.skip('not ready')\n"
        "@voci.parametrize('n', [1, 2])\n"
        "async def test_skipped(n):\n"
        "    raise AssertionError('must not run')\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert result.errors == []
    assert [skipped.id for skipped in result.skipped] == ["test_sample.py::test_skipped"]


def test_an_empty_argvalues_is_a_collection_error_not_a_silently_vanished_test(
    tmp_path: Path,
) -> None:
    """`@voci.parametrize` rejects an empty `argvalues` at decoration time (module-import time,
    from collection's point of view) rather than expanding into zero records with nothing to
    show for it."""
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n@voci.parametrize('n', [])\nasync def test_never_runs(n):\n    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert result.skipped == []
    assert len(result.errors) == 1
    assert "no argvalues" in result.errors[0].message


def test_a_parametrize_name_with_no_matching_parameter_is_a_collection_error(
    tmp_path: Path,
) -> None:
    """A typo'd `@voci.parametrize` argument name -- one that doesn't match any parameter of the
    test it decorates -- is refused at collection instead of expanding cleanly and then failing
    every case at call time with a bare `TypeError`."""
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n@voci.parametrize('typo', [1, 2])\nasync def test_x():\n    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert len(result.errors) == 1
    assert "typo" in result.errors[0].message


def test_a_parametrize_name_is_allowed_when_the_test_takes_star_kwargs(tmp_path: Path) -> None:
    """`**kwargs` absorbs any keyword, so a parametrize name with no same-named parameter is
    exactly as valid there as it would be calling the function by hand."""
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.parametrize('n', [1, 2])\n"
        "async def test_x(**kwargs):\n"
        "    assert kwargs['n'] in (1, 2)\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert len(result.records) == 2


def test_a_malformed_di_graph_is_still_a_collection_error(tmp_path: Path) -> None:
    """A test whose fixture graph fails `plan_for`'s static validation (here: a session-scoped
    fixture depending on a function-scoped one) is refused exactly like a broken import —
    attributed to the file, collection of the rest of the suite continues."""
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.fixture()\n"
        "async def narrow():\n"
        "    return 1\n\n"
        "@voci.fixture(scope='session')\n"
        "async def wide(x: int = voci.Depends(narrow)):\n"
        "    return x\n\n"
        "async def test_needs_wide(value: int = voci.Depends(wide)):\n"
        "    assert value == 1\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert result.skipped == []
    assert len(result.errors) == 1
    assert "narrow" in result.errors[0].message
    assert "wide" in result.errors[0].message


def test_a_missing_injection_is_still_a_collection_error(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "async def test_needs_something(value: int):\n    assert value == 1\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert len(result.errors) == 1
    assert "test_needs_something" in result.errors[0].message
    assert "value" in result.errors[0].message


def test_a_test_depending_on_a_parametrized_fixture_expands_into_one_record_per_case(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.fixture(params=['sqlite', 'postgres'])\n"
        "async def backend(param):\n"
        "    return param\n\n"
        "async def test_uses_backend(value: str = voci.Depends(backend)):\n"
        "    assert value in ('sqlite', 'postgres')\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.id for record in result.records] == [
        "test_sample.py::test_uses_backend[sqlite]",
        "test_sample.py::test_uses_backend[postgres]",
    ]
    # Each case gets its own specialized plan, not a shared one -- unlike @voci.parametrize.
    assert result.records[0].plan is not result.records[1].plan
    assert [record.params for record in result.records] == [None, None]


def test_a_parametrized_fixture_expands_a_transitive_dependent_too(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.fixture(params=['a', 'b'])\n"
        "async def backend(param):\n"
        "    return param\n\n"
        "@voci.fixture()\n"
        "async def engine(b: str = voci.Depends(backend)):\n"
        "    return f'engine+{b}'\n\n"
        "async def test_uses_engine(value: str = voci.Depends(engine)):\n"
        "    assert value.startswith('engine+')\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.id for record in result.records] == [
        "test_sample.py::test_uses_engine[a]",
        "test_sample.py::test_uses_engine[b]",
    ]


def test_fixture_params_and_test_level_parametrize_cross_multiply(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.fixture(params=['a', 'b'])\n"
        "async def backend(param):\n"
        "    return param\n\n"
        "@voci.parametrize('n', [1, 2])\n"
        "async def test_both(n, value: str = voci.Depends(backend)):\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.id.rsplit("[", 1)[-1] for record in result.records] == [
        "a-1]",
        "a-2]",
        "b-1]",
        "b-2]",
    ]
    assert [record.params for record in result.records] == [
        {"n": 1},
        {"n": 2},
        {"n": 1},
        {"n": 2},
    ]


def test_a_test_unrelated_to_a_parametrized_fixture_is_not_expanded(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.fixture(params=['a', 'b'])\n"
        "async def backend(param):\n"
        "    return param\n\n"
        "async def test_plain():\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.id for record in result.records] == ["test_sample.py::test_plain"]


def test_a_fixture_parametrized_with_no_param_argument_is_a_collection_error(
    tmp_path: Path,
) -> None:
    """`@voci.fixture(params=...)` is validated at decoration time -- a broken fixture module
    surfaces as a whole-file `CollectionError`, the same way any other bad decoration would."""
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.fixture(params=['a', 'b'])\n"
        "async def backend():\n"
        "    return 1\n\n"
        "async def test_never_collected():\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert len(result.errors) == 1
    assert "param" in result.errors[0].message


def test_assertion_rewrite_hook_is_consulted_when_installed(tmp_path: Path) -> None:
    """End-to-end proof that `_import_module` actually gives the installed hook a chance,
    rather than bypassing `sys.meta_path` via a bare `spec_from_file_location`: a rewritten
    module's failing `assert 2 == 3` carries the explanation text only the AST rewrite
    produces, not a bare `AssertionError`."""
    path = _write(
        tmp_path / "test_sample.py",
        "async def test_fails():\n    x = 2\n    y = 3\n    assert x == y\n",
    )

    setup = _rewrite.install([tmp_path], mode="rewrite", warn=False)
    try:
        assert not setup.degraded, setup.fallback_reason
        result = collect([path], rootdir=tmp_path)
    finally:
        _rewrite.uninstall()

    assert len(result.records) == 1
    failure = result.records[0].func
    try:
        # `func` is `Callable[..., object]` (see `_run.run_suite`'s identical cast) — `test_fails`
        # above is declared `async def`, so this particular `failure()` is a coroutine.
        asyncio.run(cast("Coroutine[Any, Any, object]", failure()))
    except AssertionError as exc:
        assert "assert 2 == 3" in str(exc)
    else:
        raise AssertionError("expected the test function to fail")


def test_a_patch_decorated_test_still_has_its_depends_defaults_injected(
    tmp_path: Path,
) -> None:
    """A decorator's wrapper takes `(*args, **kwargs)`, which hides the real signature: the plan
    is built from the function underneath, so the `Depends(...)` default is found and resolved
    rather than passed through to the test as the raw sentinel."""
    path = _write(
        tmp_path / "test_sample.py",
        "from unittest import mock\n"
        "import voci\n\n"
        "@voci.fixture()\n"
        "async def db():\n"
        "    return 1\n\n"
        "@mock.patch('os.getcwd', return_value='/x')\n"
        "async def test_patched(getcwd, value: int = voci.Depends(db)):\n"
        "    assert value == 1\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert len(result.records) == 1
    record = result.records[0]
    assert record.plan.root_args == (("value", 0, False),)
    assert record.plan.steps[0].fixture.name == "db"
    # The mock's own parameter is filled by `unittest.mock`, not by voci.
    assert record.patches == ("getcwd",)


def test_a_patch_decorated_test_records_what_it_patches(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import os\n"
        "from unittest import mock\n\n"
        "@mock.patch.dict(os.environ, {'VOCI_TEST': '1'})\n"
        "async def test_env():\n"
        "    pass\n\n"
        "async def test_plain():\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert [record.patches for record in result.records] == [("os._Environ",), ()]


def test_a_patch_multiple_test_collects_with_its_named_mock_parameters(tmp_path: Path) -> None:
    """`mock.patch.multiple` fills its parameters by name, so they are supplied rather than
    missing -- exactly like `@voci.parametrize`'s, and alongside a real injection."""
    path = _write(
        tmp_path / "test_sample.py",
        "from unittest import mock\n"
        "import voci\n\n"
        "@voci.fixture()\n"
        "async def db():\n"
        "    return 1\n\n"
        "@mock.patch.multiple('os.path', exists=mock.DEFAULT, isdir=mock.DEFAULT)\n"
        "async def test_patched(exists, isdir, value: int = voci.Depends(db)):\n"
        "    assert value == 1\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert len(result.records) == 1
    assert result.records[0].patches == ("exists", "isdir")
    assert result.records[0].plan.root_args == (("value", 0, False),)


def test_a_decorated_test_keeps_its_place_in_definition_order(tmp_path: Path) -> None:
    """Line numbers come from the function underneath the decorators -- a wrapper's own line
    number is wherever the decorator happens to be defined, which is nowhere near the test."""
    path = _write(
        tmp_path / "test_sample.py",
        "from unittest import mock\n\n"
        "async def test_first():\n"
        "    pass\n\n"
        "@mock.patch('os.getcwd')\n"
        "async def test_second(getcwd):\n"
        "    pass\n\n"
        "async def test_third():\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert [record.qualname for record in result.records] == [
        "test_first",
        "test_second",
        "test_third",
    ]
    assert [record.lineno for record in result.records] == [3, 6, 10]


def test_a_depends_default_in_a_slot_mock_patch_fills_is_a_collection_error(
    tmp_path: Path,
) -> None:
    """`unittest.mock` passes its mocks in first, positionally, so an injected parameter
    declared ahead of them would receive a mock instead of its fixture."""
    path = _write(
        tmp_path / "test_sample.py",
        "from unittest import mock\n"
        "import voci\n\n"
        "@voci.fixture()\n"
        "async def db():\n"
        "    return 1\n\n"
        "@mock.patch('os.getcwd')\n"
        "async def test_patched(value: int = voci.Depends(db), getcwd=None):\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert len(result.errors) == 1
    assert "value" in result.errors[0].message
    assert "@mock.patch" in result.errors[0].message


def test_an_ordinary_wrapping_decorator_no_longer_hides_an_injection(tmp_path: Path) -> None:
    """`functools.wraps` is what makes a decorated test collectible at all, and reading the
    plan through it is what makes its dependencies resolvable."""
    path = _write(
        tmp_path / "test_sample.py",
        "import functools\n"
        "import voci\n\n"
        "def announce(fn):\n"
        "    @functools.wraps(fn)\n"
        "    async def wrapper(*args, **kwargs):\n"
        "        return await fn(*args, **kwargs)\n"
        "    return wrapper\n\n"
        "@voci.fixture()\n"
        "async def db():\n"
        "    return 1\n\n"
        "@announce\n"
        "async def test_wrapped(value: int = voci.Depends(db)):\n"
        "    assert value == 1\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.errors == []
    assert len(result.records) == 1
    assert result.records[0].plan.root_args == (("value", 0, False),)
    assert result.records[0].patches == ()


# `voci.case(..., marks=...)`: marks that reach one case of a parametrized test.
# ------------------------------------------------------------------------------------------


def test_a_case_marked_skip_is_skipped_and_its_siblings_still_run(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.parametrize('n', [1, voci.case(2, marks=voci.skip('flaky case'))])\n"
        "async def test_it(n):\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert [record.id for record in result.records] == ["test_sample.py::test_it[1]"]
    assert [skipped.id for skipped in result.skipped] == ["test_sample.py::test_it[2]"]
    assert result.skipped[0].reason == "flaky case"
    assert result.errors == []


def test_a_cases_marks_reach_that_cases_record_only(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.parametrize('n', [1, voci.case(2, marks=voci.xfail('known'))])\n"
        "async def test_it(n):\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    expectations = {record.id: record.marks.xfail for record in result.records}
    assert expectations["test_sample.py::test_it[1]"] is None
    expected = expectations["test_sample.py::test_it[2]"]
    assert expected is not None and expected.reason == "known"


def test_a_case_with_marks_of_its_own_does_not_re_evaluate_the_functions_own_condition(
    tmp_path: Path,
) -> None:
    """A case carrying an unrelated mark of its own (here, a tag) still folds the function's
    `xfail` into its record -- but must not call its condition a second time to do it, since
    `decided` promises callers that evaluating it once is the whole contract."""
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "calls = []\n\n"
        "def _counted():\n"
        "    calls.append(1)\n"
        "    return True\n\n"
        "@voci.xfail('known', condition=_counted)\n"
        "@voci.parametrize('n', [1, voci.case(2, marks=voci.tag('slow'))])\n"
        "async def test_it(n):\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert len(result.records) == 2
    assert all(record.marks.xfail is not None for record in result.records)
    calls = result.records[0].func.__globals__["calls"]
    assert calls == [1]


def test_a_tag_on_one_case_selects_that_case_alone(tmp_path: Path) -> None:
    """`-m` is answered per case where the cases differ: the function's own tags cannot decide
    for a test one of whose cases is tagged and the rest are not."""
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.parametrize('n', [1, voci.case(2, marks=voci.tag('slow'))])\n"
        "async def test_it(n):\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path, tag_expr=compile_tag_expression("slow"))

    assert [record.id for record in result.records] == ["test_sample.py::test_it[2]"]
    assert result.deselected == ["test_sample.py::test_it[1]"]


def test_a_tagged_case_is_deselected_by_a_negated_tag_expression(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.parametrize('n', [1, voci.case(2, marks=voci.tag('slow'))])\n"
        "async def test_it(n):\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path, tag_expr=compile_tag_expression("not slow"))

    assert [record.id for record in result.records] == ["test_sample.py::test_it[1]"]
    assert result.deselected == ["test_sample.py::test_it[2]"]


def test_a_case_condition_that_raises_is_one_collection_error_for_the_test(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "def _boom():\n"
        "    raise RuntimeError('condition boom')\n\n"
        "@voci.parametrize('n', [1, voci.case(2, marks=voci.skipif(_boom, reason='x'))])\n"
        "async def test_it(n):\n"
        "    pass\n",
    )

    result = collect([path], rootdir=tmp_path)

    assert result.records == []
    assert len(result.errors) == 1
    assert "condition boom" in result.errors[0].message


def test_a_records_marks_are_the_functions_own_where_no_case_carries_any(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n@voci.timeout(5)\nasync def test_it():\n    pass\n",
    )

    (record,) = collect([path], rootdir=tmp_path).records

    assert record.marks.timeout == 5


def test_an_xfail_whose_condition_does_not_hold_is_not_on_the_record(tmp_path: Path) -> None:
    """Decided once, at collection: the runner asks whether there is an expectation, never
    whether one applies."""
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.xfail('only elsewhere', condition=False)\n"
        "async def test_it():\n"
        "    pass\n",
    )

    (record,) = collect([path], rootdir=tmp_path).records

    assert record.marks.xfail is None


def test_an_xfail_condition_is_evaluated_at_collection_not_at_import(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "test_sample.py",
        "import voci\n\n"
        "@voci.xfail('computed', condition=lambda: True)\n"
        "async def test_it():\n"
        "    pass\n",
    )

    (record,) = collect([path], rootdir=tmp_path).records

    assert record.marks.xfail is not None
    assert record.marks.xfail.reason == "computed"
