"""Tests for velox_migrate.convert: the rewrite, end to end and in its parts.

The end-to-end tests are the ones that matter. A conversion is only correct if its output runs, so
the mechanical corpus suite is converted into a temporary tree, executed under velox, and compared
test-for-test against what pytest collected — and then converted a second time, where changing
nothing is the property that makes a conversion survivable on a branch that keeps moving.

Everything runs against the checked-in dump from each supported pytest, since a conversion driven
by 8.4's answers and one driven by 9.1's must place the same objects in the same files.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import libcst as cst
import pytest
from _support import CORPUS, DUMPS, conversion_of, converted

from velox_migrate import audit, convert, matrix, model
from velox_migrate.convert import annotate, config, layout, markers, parametrize, plan, wiring
from velox_migrate.convert.layout import Import
from velox_migrate.report import conversion as conversion_report

STANDALONE = "test_it.py"

BODIES = "bodies_showcase"
CLASSES = "classes_showcase"
MECHANICAL = "mechanical_showcase"
DECLARATIONS = "declarations_showcase"
FIXTURES = "fixtures_showcase"
HAZARDS = "hazards_showcase"
OVERRIDES = "overrides_showcase"
PARAMETRIZE = "parametrize_showcase"
TYPED = "typed_showcase"


def ids_under(runner: list[str], tree: Path) -> list[str]:
    """Every node id `runner` collects in `tree`, sorted.

    Both runners list one id per line under `--collect-only -q`; velox follows a skipped test's id
    with its reason, so a line is cut at its first space.
    """
    completed = subprocess.run(
        [*runner, "--collect-only", "-q", str(tree)],
        capture_output=True,
        text=True,
        check=False,
        cwd=tree,
    )
    lines = [line.strip().split(" ")[0] for line in completed.stdout.splitlines()]
    return sorted(line for line in lines if "::" in line and not line.startswith(("=", "-")))


VELOX = [sys.executable, "-c", "import sys; from velox.cli import main; sys.exit(main())"]
PYTEST = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"]


# --- the exit bar -----------------------------------------------------------------------------


def test_the_mechanical_suite_converts_with_nothing_refused(version: str) -> None:
    result = conversion_of(MECHANICAL, version)

    assert result.plan.blocked_tests == frozenset()
    assert result.plan.blocked_fixtures == frozenset()
    assert result.refused == ()


def test_the_converted_mechanical_suite_passes_under_velox(version: str, tmp_path: Path) -> None:
    tree = tmp_path / MECHANICAL
    converted(MECHANICAL, version, tree)

    completed = subprocess.run(
        [*VELOX, "--serial", str(tree)], capture_output=True, text=True, check=False, cwd=tree
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_the_declarations_suite_converts_with_nothing_refused(version: str) -> None:
    result = conversion_of(DECLARATIONS, version)

    assert result.plan.blocked_tests == frozenset()
    assert result.plan.blocked_fixtures == frozenset()
    assert result.refused == ()


def test_the_converted_declarations_suite_passes_under_velox(version: str, tmp_path: Path) -> None:
    # The bar for declarations specifically: every fixture these tests get without naming one is
    # constructed for them, in the order pytest constructed it, through a package chain that was
    # not there before the conversion wrote it.
    tree = tmp_path / DECLARATIONS
    converted(DECLARATIONS, version, tree)

    completed = subprocess.run(
        [*VELOX, "--serial", str(tree)], capture_output=True, text=True, check=False, cwd=tree
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_the_converted_declarations_suite_keeps_every_pytest_node_id(
    version: str, tmp_path: Path
) -> None:
    tree = tmp_path / DECLARATIONS
    converted(DECLARATIONS, version, tree)

    assert ids_under(VELOX, tree) == ids_under(PYTEST, CORPUS / DECLARATIONS)


def test_the_bodies_suite_converts_with_nothing_refused(version: str) -> None:
    result = conversion_of(BODIES, version)

    assert result.plan.blocked_tests == frozenset()
    assert result.plan.blocked_fixtures == frozenset()
    assert result.refused == ()


def test_the_converted_bodies_suite_passes_under_velox(version: str, tmp_path: Path) -> None:
    # The bar for the body rewrites: a fixture asked for by name is constructed for the test that
    # asked, a finalizer runs at teardown in the order pytest ran it, and a test that patches
    # inside its body holds the whole run while it does.
    tree = tmp_path / BODIES
    converted(BODIES, version, tree)

    completed = subprocess.run(
        [*VELOX, "--serial", str(tree)], capture_output=True, text=True, check=False, cwd=tree
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_the_converted_bodies_suite_keeps_every_pytest_node_id(
    version: str, tmp_path: Path
) -> None:
    tree = tmp_path / BODIES
    converted(BODIES, version, tree)

    assert ids_under(VELOX, tree) == ids_under(PYTEST, CORPUS / BODIES)


def test_a_fixture_asked_for_by_name_becomes_a_parameter_of_the_definition_that_asked(
    version: str, tmp_path: Path
) -> None:
    # The name was a literal, so the dependency was static all along: `request` had nothing else
    # to do here and goes with it.
    tree = tmp_path / BODIES
    converted(BODIES, version, tree)

    body = (tree / "fixtures.py").read_text(encoding="utf-8")

    assert "def engine(settings: Annotated[Any, Depends(settings)]):" in body
    assert 'request.getfixturevalue("settings")' not in body


def test_a_test_that_asked_for_a_fixture_by_name_loses_its_request_parameter(
    version: str, tmp_path: Path
) -> None:
    tree = tmp_path / BODIES
    converted(BODIES, version, tree)

    body = (tree / "test_bodies.py").read_text(encoding="utf-8")

    assert "def test_getfixturevalue_by_name(settings: Annotated[Any, Depends(settings)]):" in body


def test_an_unconditional_finalizer_becomes_the_teardown_after_a_yield(
    version: str, tmp_path: Path
) -> None:
    tree = tmp_path / BODIES
    converted(BODIES, version, tree)

    body = (tree / "fixtures.py").read_text(encoding="utf-8")
    journal = body[body.index("def journal") :]

    assert "request.addfinalizer(" not in body
    # pytest runs the last-registered finalizer first, and a `yield` fixture tears down top to
    # bottom, so the calls are written in the reverse of the order they were registered in.
    assert journal.index("yield {") < journal.index('append("journal-inner")')
    assert journal.index('append("journal-inner")') < journal.index('append("journal-outer")')


def test_a_patch_entered_inside_a_body_puts_the_test_on_its_own(
    version: str, tmp_path: Path
) -> None:
    tree = tmp_path / BODIES
    converted(BODIES, version, tree)

    body = (tree / "test_bodies.py").read_text(encoding="utf-8")

    assert "@velox.solo\ndef test_patch_context_manager(" in body
    assert "@velox.solo\ndef test_started_patcher(" in body


def test_a_patch_applied_as_a_decorator_keeps_it_and_is_marked_by_nothing(
    version: str, tmp_path: Path
) -> None:
    # velox finds a decorator's patching on the function object at collection and schedules that
    # test alone, so the conversion writes no mark of its own — and the injected parameters go
    # after the mock the decorator fills positionally.
    tree = tmp_path / BODIES
    converted(BODIES, version, tree)

    body = (tree / "test_bodies.py").read_text(encoding="utf-8")

    signature = "def test_patch_decorator(getcwd, engine: Annotated[Any, Depends(engine)]):"
    decorated = f'@mock.patch("os.getcwd")\n{signature}'

    assert decorated in body
    assert "@velox.solo\n@mock.patch" not in body


def test_a_specialized_copy_is_rewritten_the_way_the_fixture_it_copies_is(
    version: str, tmp_path: Path
) -> None:
    # A copy is the original's source under another name, so the plan's answers about that body
    # are the copy's answers too: the name it asked for is injected into it, with the import that
    # reference needs, and its finalizer is the teardown after its `yield`.
    tree = tmp_path / BODIES
    converted(BODIES, version, tree)

    body = (tree / "sub" / "fixtures.py").read_text(encoding="utf-8")

    assert "from fixtures import finished, settings" in body
    assert "def report_sub(" in body
    assert "settings: Annotated[Any, Depends(settings)]" in body
    assert "request" not in body


def test_a_name_resolving_to_a_fixture_nothing_writes_an_object_for_is_refused() -> None:
    # `engine` asks for `settings` by name. Told that `settings` comes from an installed plugin
    # rather than from this suite, there is no object a `Depends()` could name — so the fixture
    # keeps the `request` it asked through, and the tests that reach it keep their pytest source.
    dump = json.loads((DUMPS / f"{BODIES}-pytest-9.1.json").read_text(encoding="utf-8"))
    key = next(k for k, entry in dump["fixture_defs"].items() if entry["argname"] == "settings")
    dump["fixture_defs"][key]["func"]["file"] = "${prefix}/site-packages/plugin.py"
    ground_truth = model.build(dump)
    root = CORPUS / BODIES

    result = convert.run(audit.run(ground_truth, root=root), ground_truth, root=root)

    blocked = {ground_truth.fixture_defs[found].argname for found in result.plan.blocked_fixtures}

    assert "engine" in blocked
    assert "test_bodies.py::test_getfixturevalue_by_name" in result.plan.blocked_tests


def test_the_overrides_suite_converts_with_nothing_refused(version: str) -> None:
    result = conversion_of(OVERRIDES, version)

    assert result.plan.blocked_tests == frozenset()
    assert result.plan.blocked_fixtures == frozenset()
    assert result.refused == ()


def test_the_converted_overrides_suite_passes_under_velox(version: str, tmp_path: Path) -> None:
    # The bar for specialization: a name that meant two things depending on where a test lived is
    # two objects here, and each test gets the one its own directory resolved.
    tree = tmp_path / OVERRIDES
    converted(OVERRIDES, version, tree)

    completed = subprocess.run(
        [*VELOX, "--serial", str(tree)], capture_output=True, text=True, check=False, cwd=tree
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_the_converted_overrides_suite_keeps_every_pytest_node_id(
    version: str, tmp_path: Path
) -> None:
    tree = tmp_path / OVERRIDES
    converted(OVERRIDES, version, tree)

    assert ids_under(VELOX, tree) == ids_under(PYTEST, CORPUS / OVERRIDES)


def _target(result: convert.Conversion, path: str) -> str:
    """What the conversion writes to `path`, for the assertions that read the output directly."""
    return next(edit.new_text or "" for edit in result.edits.edits if edit.path == path)


def test_the_classes_suite_converts_with_nothing_refused(version: str) -> None:
    result = conversion_of(CLASSES, version)

    assert result.plan.blocked_tests == frozenset()
    assert result.plan.blocked_fixtures == frozenset()
    assert result.refused == ()


def test_the_converted_classes_suite_passes_under_velox(version: str, tmp_path: Path) -> None:
    # The bar for lifting: a velox test class has no fixtures, so every factory written in one is
    # a module-level object by the time the class's own methods are constructed.
    tree = tmp_path / CLASSES
    converted(CLASSES, version, tree)

    completed = subprocess.run(
        [*VELOX, "--serial", str(tree)], capture_output=True, text=True, check=False, cwd=tree
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_the_converted_classes_suite_keeps_every_pytest_node_id(
    version: str, tmp_path: Path
) -> None:
    tree = tmp_path / CLASSES
    converted(CLASSES, version, tree)

    assert ids_under(VELOX, tree) == ids_under(PYTEST, CORPUS / CLASSES)


def test_a_lifted_fixture_is_named_for_its_class_and_written_above_it(version: str) -> None:
    # Two classes bind `base`, which one module level cannot, and a `Depends()` default is read
    # while the class body runs, which is why the order matters as much as the name.
    source = _target(conversion_of(CLASSES, version), "test_classes.py")

    assert "def siblings_base(" in source
    assert "def same_name_base(" in source
    assert source.index("def siblings_derived(") > source.index("def siblings_base(")
    assert source.index("class TestSiblings:") > source.index("def siblings_derived(")


def test_a_lifted_fixture_reads_a_class_attribute_through_the_class(version: str) -> None:
    source = _target(conversion_of(CLASSES, version), "test_classes.py")

    assert "return TestSiblings.STAMP" in source
    # The `self` a nested `def` declares is that function's, and the move leaves it alone.
    assert "return self.marker" in source


def test_a_chain_specialized_for_a_class_is_wired_copy_to_copy(version: str) -> None:
    # Every link between the override and the tests that reach it is copied, and each copy names
    # the copy below it rather than the definition it was written against.
    source = _target(conversion_of(CLASSES, version), "test_classes.py")

    assert "def blog_overriding(user: Annotated[Any, Depends(overriding_user)]):" in source
    assert "def digest_overriding(blog: Annotated[Any, Depends(blog_overriding)]):" in source
    assert source.index("def digest_overriding(") < source.index("class TestOverriding:")


def test_a_module_imports_only_the_fixtures_its_own_code_names(version: str) -> None:
    # `digest` is reached only through the copy written here, so importing the original would be
    # an import nothing in the module reads.
    source = _target(conversion_of(CLASSES, version), "test_classes.py")

    imported = [line for line in source.splitlines() if line.startswith(("import ", "from "))]

    assert "from fixtures import blog" in imported
    assert not [line for line in imported if "digest" in line]


def test_the_parametrize_suite_converts_with_nothing_refused(version: str) -> None:
    result = conversion_of(PARAMETRIZE, version)

    assert result.plan.blocked_tests == frozenset()
    assert result.plan.blocked_fixtures == frozenset()
    assert result.refused == ()


def test_the_converted_parametrize_suite_passes_under_velox(version: str, tmp_path: Path) -> None:
    # The bar for the two call-site parametrizations: a fixture an `indirect` mark chose cases for
    # builds each of them, and a test a hook built cases for runs the list the hook produced.
    tree = tmp_path / PARAMETRIZE
    converted(PARAMETRIZE, version, tree)

    completed = subprocess.run(
        [*VELOX, "--serial", str(tree)], capture_output=True, text=True, check=False, cwd=tree
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_the_converted_parametrize_suite_keeps_every_pytest_node_id(
    version: str, tmp_path: Path
) -> None:
    tree = tmp_path / PARAMETRIZE
    converted(PARAMETRIZE, version, tree)

    assert ids_under(VELOX, tree) == ids_under(PYTEST, CORPUS / PARAMETRIZE)


def test_an_indirect_marks_values_become_the_fixtures_own_cases(
    version: str, tmp_path: Path
) -> None:
    # The mark is written in the test module and the `params=` is read in the fixture module, so
    # what travels is the value pytest passed — with the ids it composed, which is what keeps the
    # node ids. Both tests reaching `backend` lose the mark, including the one that never named it.
    tree = tmp_path / PARAMETRIZE
    converted(PARAMETRIZE, version, tree)

    fixtures = (tree / "fixtures.py").read_text(encoding="utf-8")
    tests = (tree / "test_indirect.py").read_text(encoding="utf-8")

    assert '@velox.fixture(params=["mysql", "sqlite"], ids=["mysql", "sqlite"])' in fixtures
    assert "def backend(param):" in fixtures
    assert "indirect" not in tests
    assert "def test_backend_through_engine(engine: Annotated[Any, Depends(engine)]):" in tests


def test_a_hook_that_built_cases_leaves_them_written_on_each_test(
    version: str, tmp_path: Path
) -> None:
    tree = tmp_path / PARAMETRIZE
    converted(PARAMETRIZE, version, tree)

    body = (tree / "test_generated.py").read_text(encoding="utf-8")

    assert '@velox.parametrize("width,height", [(2, 3), (5, 8)], ids=["small", "large"])' in body
    assert '@velox.parametrize("letter", ["a", "b"], ids=["a", "b"])' in body
    assert "VELOX-TODO[VX024]" in body


def test_an_indirect_mark_on_a_fixture_with_cases_of_its_own_refuses_the_test(
    version: str, tmp_path: Path
) -> None:
    # `backend` is a `params=` fixture that one test also parametrizes indirectly. One case list
    # cannot be both, so the fixture keeps the cases every other test reaches it for and the test
    # that asked for different ones keeps its pytest source.
    tree = tmp_path / FIXTURES
    result = converted(FIXTURES, version, tree)

    body = (tree / "test_top.py").read_text(encoding="utf-8")

    assert [f.code for f in result.plan.refusals if f.code == "VX029"] == ["VX029"]
    assert "test_top.py::test_indirect[mysql]" in result.plan.blocked_tests
    assert '@pytest.mark.parametrize("backend", ["mysql"], indirect=True)' in body
    assert "VELOX-TODO[VX029]" in body
    assert 'ids=["lite", "pg"]' in (tree / "fixtures.py").read_text(encoding="utf-8")


def test_a_generated_value_with_no_literal_spelling_refuses_the_test() -> None:
    # A frozen case list is written from the value each case was given, and a `repr` that reads
    # back as nothing is not a value this can write down.
    dump = json.loads((DUMPS / f"{PARAMETRIZE}-pytest-9.1.json").read_text(encoding="utf-8"))
    for entry in dump["items"]:
        params = (entry.get("callspec") or {}).get("params", {})
        if "letter" in params:
            params["letter"] = "<Backend object at 0x1>"
    ground_truth = model.build(dump)
    root = CORPUS / PARAMETRIZE

    result = convert.run(audit.run(ground_truth, root=root), ground_truth, root=root)

    assert [f.code for f in result.plan.refusals] == ["VX031"]
    assert "test_generated.py::test_letters[a]" in result.plan.blocked_tests
    assert "test_generated.py::test_area[small]" not in result.plan.blocked_tests


def test_the_converted_suite_keeps_every_pytest_node_id(version: str, tmp_path: Path) -> None:
    # §9's promise, and why `@velox.parametrize` is emitted with pytest's own ids: a CI config, a
    # flaky-test dashboard or a `--last-failed` habit that names an id keeps working.
    tree = tmp_path / MECHANICAL
    converted(MECHANICAL, version, tree)

    assert ids_under(VELOX, tree) == ids_under(PYTEST, CORPUS / MECHANICAL)


def test_stacked_parametrize_marks_are_reversed_so_their_composed_ids_hold(
    version: str, tmp_path: Path
) -> None:
    # pytest applies decorators bottom-up, so its innermost axis varies slowest and is written
    # first in the id; velox reads its own list outermost-first. Reversing is what makes the two
    # agree, and it changes nothing else — the cases are the same product either way.
    tree = tmp_path / MECHANICAL
    converted(MECHANICAL, version, tree)

    body = (tree / "test_marks.py").read_text(encoding="utf-8")
    stacked = body[body.index("def test_parametrize_stacked") - 200 :]

    assert stacked.index('@velox.parametrize("inner"') < stacked.index('@velox.parametrize("outer"')
    assert "test_marks.py::test_parametrize_stacked[i1-o2]" in ids_under(VELOX, tree)


def test_ids_composed_from_several_axes_are_reported_even_where_they_hold(
    version: str, tmp_path: Path
) -> None:
    # The ids match, but only because every axis here is a string velox spells the same way. No
    # `ids=` could be written onto either mark, so the report says so rather than relying on that.
    tree = tmp_path / MECHANICAL
    result = converted(MECHANICAL, version, tree)

    reported = {record.qualname for record in result.applied if record.code == "VX114"}

    assert reported == {"test_parametrize_stacked", "test_parametrize_single_case_axes"}


def test_an_axis_of_its_own_carries_pytests_ids_verbatim(version: str, tmp_path: Path) -> None:
    # The whole point of emitting `ids=`: a float and a tuple are exactly where pytest's generated
    # ids and velox's diverge, and copying pytest's closes the gap rather than documenting it.
    tree = tmp_path / MECHANICAL
    converted(MECHANICAL, version, tree)

    collected = ids_under(VELOX, tree)

    assert "test_marks.py::test_parametrize_generated_ids[0.25]" in collected
    assert "test_marks.py::test_parametrize_two_argnames[None-True]" in collected


@pytest.mark.parametrize(
    "suite", [MECHANICAL, DECLARATIONS, OVERRIDES, BODIES, PARAMETRIZE, CLASSES]
)
def test_converting_an_already_converted_tree_changes_nothing(
    suite: str, version: str, tmp_path: Path
) -> None:
    tree = tmp_path / suite
    converted(suite, version, tree)

    again = conversion_of(suite, version, root=tree)

    assert again.edits.diff() == ""


@pytest.mark.parametrize("suite", [FIXTURES, HAZARDS])
def test_a_partial_conversion_is_idempotent_too(suite: str, version: str, tmp_path: Path) -> None:
    # A suite full of refusals still has to be re-runnable: the markers it wrote are the thing
    # most likely to be written twice, since nothing about them is a pytest source form.
    tree = tmp_path / suite
    converted(suite, version, tree)

    again = conversion_of(suite, version, root=tree)

    assert again.edits.diff() == ""


# --- layout -----------------------------------------------------------------------------------


def test_a_conftest_fixture_moves_to_the_fixtures_module_beside_it(version: str) -> None:
    result = conversion_of(FIXTURES, version)

    assert result.plan.layout.moves == {
        "conftest.py": "fixtures.py",
        "integration/conftest.py": "integration/fixtures.py",
    }


def test_a_fixture_written_under_a_decorator_is_placed_by_the_conftest_that_owns_it(
    version: str,
) -> None:
    # `opaque_fix`'s factory is recorded in `helpers.py`, because the decorator wrapping it left no
    # trail back; the conftest is still where the name is bound and where it has to be imported
    # from.
    ground_truth = model.load(DUMPS / f"{FIXTURES}-pytest-{version}.json")
    opaque = next(
        fixture for fixture in ground_truth.fixture_defs.values() if fixture.argname == "opaque_fix"
    )

    assert opaque.func.file == "helpers.py"
    assert layout.owning_file(opaque) == "conftest.py"


def test_a_globally_registered_fixture_has_no_owning_file(version: str) -> None:
    ground_truth = model.load(DUMPS / f"{FIXTURES}-pytest-{version}.json")
    builtin = next(
        fixture for fixture in ground_truth.fixture_defs.values() if fixture.argname == "tmp_path"
    )

    assert layout.owning_file(builtin) is None


def test_an_import_is_absolute_and_rootdir_relative() -> None:
    assert layout.dotted("tests/integration/fixtures.py") == "tests.integration.fixtures"
    assert layout.dotted("fixtures.py") == "fixtures"


def test_an_import_colliding_with_a_name_the_module_binds_is_aliased() -> None:
    sources = {
        "integration/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef client():\n    ...\n",
        "test_it.py": "def client():\n    return 1\n",
    }

    placed = layout.plan(
        {"k": _fixture_def("client", "integration")},
        symbols={("integration/conftest.py", "client"): "client"},
        consumers={"test_it.py": ["k"]},
        source_of=sources.get,
    )

    assert [str(item) for item in placed.imports_for("test_it.py")] == [
        "from integration.fixtures import client as integration_client"
    ]


def _fixture_def(
    argname: str,
    visibility: str,
    *,
    returns: str | None = None,
    file: str | None = None,
) -> model.FixtureDef:
    return model.FixtureDef(
        key=argname,
        argname=argname,
        scope="function",
        params=None,
        ids=None,
        autouse=False,
        visibility=visibility,
        kind="function",
        direct_param=False,
        argnames=(),
        returns=returns,
        func=model.FuncLocation(
            module=None,
            qualname=argname,
            file=file if file is not None else f"{visibility}/conftest.py",
            lineno=1,
            wrapped=False,
        ),
    )


def test_the_alias_names_the_directory_the_fixture_came_from() -> None:
    item = Import("tests.integration.fixtures", "client", "integration_client")

    assert str(item) == "from tests.integration.fixtures import client as integration_client"


# --- refusal --------------------------------------------------------------------------------


def test_an_override_is_specialized_rather_than_refused(version: str) -> None:
    # `engine` is written once and needs no rewrite of its own, but under `integration/` it
    # resolves a `settings` that has two definitions — so it becomes two objects, one per
    # definition, and nothing about it is refused.
    result = conversion_of(FIXTURES, version)
    ground_truth = model.load(DUMPS / f"{FIXTURES}-pytest-{version}.json")
    blocked = {ground_truth.fixture_defs[key].argname for key in result.plan.blocked_fixtures}

    assert not {"settings", "engine"} & blocked
    assert {copy.symbol for copy in result.plan.specialized.copies.values()} == {
        "engine_integration"
    }


def test_a_refusal_travels_to_the_tests_that_reach_it(version: str) -> None:
    # `dyn` reads a fixture by a name decided at run time, which nothing static resolves, so the
    # fixture is left alone and with it the test that requests it.
    result = conversion_of(FIXTURES, version)
    ground_truth = model.load(DUMPS / f"{FIXTURES}-pytest-{version}.json")
    blocked = {ground_truth.fixture_defs[key].argname for key in result.plan.blocked_fixtures}

    assert blocked == {"dyn"}
    assert "test_top.py::test_uses" in result.plan.blocked_tests


def test_a_name_the_suite_defines_twice_is_not_one_a_parameter_can_carry(
    version: str, tmp_path: Path
) -> None:
    # `literal` asks for `settings` by name, and `settings` means one fixture at the root and
    # another under `deep/`. A parameter names one object, so the fixture is left as it was.
    tree = tmp_path / HAZARDS
    result = converted(HAZARDS, version, tree)
    ground_truth = model.load(DUMPS / f"{HAZARDS}-pytest-{version}.json")
    blocked = {ground_truth.fixture_defs[key].argname for key in result.plan.blocked_fixtures}
    body = (tree / "fixtures.py").read_text(encoding="utf-8")

    assert "literal" in blocked
    assert "VELOX-TODO[VX028]" in body
    assert 'return request.getfixturevalue("settings")' in body


def test_a_request_the_rewrite_cannot_empty_leaves_the_finalizer_where_it_was(
    version: str, tmp_path: Path
) -> None:
    # `sometimes_closed` registers its finalizer under an `if`, which a `yield` fixture's teardown
    # cannot be, so both the registration and the `request` that made it stay.
    tree = tmp_path / HAZARDS
    converted(HAZARDS, version, tree)

    body = (tree / "fixtures.py").read_text(encoding="utf-8")

    assert "def sometimes_closed(request, node_name):" in body
    assert "VELOX-TODO[VX014]" in body


def test_an_unconditional_finalizer_beside_no_other_request_use_converts(
    version: str, tmp_path: Path
) -> None:
    tree = tmp_path / HAZARDS
    converted(HAZARDS, version, tree)

    body = (tree / "fixtures.py").read_text(encoding="utf-8")

    assert "def closed():" in body
    assert "yield handle" in body


def test_a_refused_test_keeps_its_pytest_signature(version: str, tmp_path: Path) -> None:
    tree = tmp_path / HAZARDS
    converted(HAZARDS, version, tree)

    body = (tree / "test_shapes.py").read_text(encoding="utf-8")

    assert "def test_cases(value):" in body
    assert "@pytest.mark.parametrize(" in body


def test_a_supported_builtin_fixture_refuses_nothing(version: str) -> None:
    # Every builtin the matrix marks mechanical converts, so a test requesting one is never
    # blocked on its account.
    result = conversion_of(HAZARDS, version)
    ground_truth = model.load(DUMPS / f"{HAZARDS}-pytest-{version}.json")
    blocked = {
        ground_truth.fixture_defs[key].argname
        for key in result.plan.blocked_fixtures
        if key in ground_truth.fixture_defs
    }

    assert "tmp_path" not in blocked
    assert "tmpdir" not in blocked
    assert "capsys" not in blocked


def test_a_class_scoped_fixture_widens_and_says_so(version: str, tmp_path: Path) -> None:
    tree = tmp_path / HAZARDS
    converted(HAZARDS, version, tree)

    body = (tree / "fixtures.py").read_text(encoding="utf-8")

    assert '@velox.fixture(scope="module")' in body
    assert "VELOX-TODO[VX003]" in body


# --- markers --------------------------------------------------------------------------------


def test_a_marker_names_the_matrix_code() -> None:
    assert markers.comment("VX102").startswith("# VELOX-TODO[VX102]: ")


def test_a_marker_is_written_once_however_often_it_is_asked_for() -> None:
    module = cst.parse_module("def test_it():\n    pass\n")

    once = markers.mark(module, {"test_it": ["VX102"]})
    twice = markers.mark(once, {"test_it": ["VX102"]})

    assert once.code.count("VELOX-TODO") == 1
    assert twice.code == once.code


def test_a_marker_on_a_method_is_indented_with_it() -> None:
    module = cst.parse_module("class TestIt:\n    def test_one(self):\n        pass\n")

    marked = markers.mark(module, {"TestIt.test_one": ["VX102"]})

    assert "\n    # VELOX-TODO[VX102]" in marked.code


def test_a_marker_about_the_module_lands_after_its_docstring() -> None:
    module = cst.parse_module('"""Doc."""\n\nimport pytest\n')

    marked = markers.mark(module, {"": ["VX022"]})

    lines = marked.code.splitlines()
    assert lines[0] == '"""Doc."""'
    assert "VELOX-TODO[VX022]" in lines[2]


# --- case ids -------------------------------------------------------------------------------


def test_a_single_parametrize_axis_carries_pytests_own_ids(version: str) -> None:
    result = conversion_of(MECHANICAL, version)
    context = result.plan.work["test_marks.py"].context

    assert context.axis_ids[("test_parametrize_generated_ids", "value")] == (
        "0.25",
        "value1",
        "explicit",
    )


def test_configuration_is_translated_into_the_table_velox_reads(version: str) -> None:
    result = conversion_of(HAZARDS, version)

    assert result.settings.settings == {"filterwarnings": '["ignore::DeprecationWarning"]'}
    assert "xfail_strict" in result.settings.dropped
    assert "addopts" in result.settings.dropped


def test_the_deferred_codes_are_all_rows_the_matrix_says_convert() -> None:
    # A code in `DEFERRED` that the matrix already refuses would be reported twice, under two
    # different explanations, for one construct.
    for code in plan.DEFERRED:
        assert matrix.construct(code).converts, code


# --- what a rewrite backs out of ----------------------------------------------------------------


def test_a_params_fixture_keeps_the_order_request_was_written_in(
    version: str, tmp_path: Path
) -> None:
    # An injection is metadata, not a default, so `param` stays where `request` was written even
    # though the fixture beside it is injected.
    tree = tmp_path / MECHANICAL
    converted(MECHANICAL, version, tree)

    body = (tree / "fixtures.py").read_text(encoding="utf-8")

    assert "def retries(dsn: Annotated[Any, Depends(dsn)], param):" in body


def test_a_parameter_asked_for_by_name_goes_before_one_the_source_gave_a_default() -> None:
    # The only parameter a rewritten signature can hold a default for is one the source wrote,
    # and a parameter without a default cannot follow it.
    source = "def test_x(request, flag=True):\n    assert db and flag\n"
    work = plan.FileWork(
        path=STANDALONE,
        target=STANDALONE,
        tests=(
            plan.TestWork(
                qualname="test_x",
                injections=(
                    plan.Injection("request", "", ""),
                    plan.Injection("db", "db", "db", asked=True),
                ),
            ),
        ),
    )

    result = wiring.apply(cst.parse_module(source), work)

    assert "def test_x(db: Annotated[Any, Depends(db)], flag=True):" in result.module.code


def test_a_parameter_asked_for_by_name_goes_behind_a_star_where_nothing_else_is_valid() -> None:
    # A defaulted positional-only parameter leaves a positional one without a default nowhere to
    # go, at either end. velox binds by keyword, so the new parameter becomes keyword-only.
    source = "def test_x(a=1, /, flag=2):\n    assert db\n"
    work = plan.FileWork(
        path=STANDALONE,
        target=STANDALONE,
        tests=(
            plan.TestWork(
                qualname="test_x", injections=(plan.Injection("db", "db", "db", asked=True),)
            ),
        ),
    )

    result = wiring.apply(cst.parse_module(source), work)

    assert "def test_x(a=1, /, flag=2, *, db: Annotated[Any, Depends(db)]):" in result.module.code


def test_a_positional_only_parameter_refuses_the_definition_under_its_own_row() -> None:
    # velox binds by keyword, so the parameter can never be given the fixture, and the signature
    # is left alone. The row says that, rather than the one about `request` outliving setup.
    source = "def test_x(db, /, flag=True):\n    assert db and flag\n"
    work = plan.FileWork(
        path=STANDALONE,
        target=STANDALONE,
        tests=(plan.TestWork(qualname="test_x", injections=(plan.Injection("db", "db", "db"),)),),
    )

    result = wiring.apply(cst.parse_module(source), work)

    assert result.refused == (("test_x", "VX036"),)
    assert result.module.code == source


def test_a_keyword_only_parameter_is_injected_where_it_was_written() -> None:
    # Keyword-only parameters bind by name whatever order they are written in, so one without a
    # default following one that has it is a signature to leave alone.
    source = "def test_x(*, flag=True, db):\n    assert db and flag\n"
    work = plan.FileWork(
        path=STANDALONE,
        target=STANDALONE,
        tests=(plan.TestWork(qualname="test_x", injections=(plan.Injection("db", "db", "db"),)),),
    )

    result = wiring.apply(cst.parse_module(source), work)

    assert "def test_x(*, flag=True, db: Annotated[Any, Depends(db)]):" in result.module.code


def test_a_test_needing_no_injection_does_not_import_depends(version: str, tmp_path: Path) -> None:
    tree = tmp_path / MECHANICAL
    converted(MECHANICAL, version, tree)

    body = (tree / "test_marks.py").read_text(encoding="utf-8")

    assert "import velox" in body
    assert "Depends" not in body


def test_a_capsys_use_no_rule_rewrites_refuses_the_test_rather_than_stranding_it() -> None:
    # `capsys` is renamed with its parameter because `velox.capture` is a different object. A use
    # the body rules decline would be left reading a name nothing binds, so the whole test stays as
    # pytest wrote it and says why.
    source = "def test_x(capsys):\n    assert capsys.readouterr()[0]\n"
    work = plan.FileWork(
        path=STANDALONE,
        target=STANDALONE,
        tests=(
            plan.TestWork(
                qualname="test_x",
                injections=(plan.Injection("capsys", "capture", "velox.capture"),),
            ),
        ),
    )

    result = wiring.apply(cst.parse_module(source), work)

    assert result.refused == (("test_x", "VX202"),)
    assert result.module.code == source


def test_a_carried_case_list_replaces_what_the_decorator_said_about_cases() -> None:
    # pytest tolerates an `ids=` with no `params=` beside it, and velox raises on one. The cases
    # are the mark's now, so what the decorator said about cases of its own goes with it.
    source = """import pytest


@pytest.fixture(ids=["only"])
def backend(request):
    return request.param
"""
    work = plan.FileWork(
        path=STANDALONE,
        target=STANDALONE,
        fixtures=(
            plan.FixtureWork(
                key="f0",
                argname="backend",
                symbol="backend",
                scope="function",
                injections=(plan.Injection("request", "param", ""),),
                parametrized=True,
                carried=parametrize.Carried(values=("'mysql'",), ids=("mysql",)),
            ),
        ),
    )

    result = wiring.apply(cst.parse_module(source), work)

    assert '@velox.fixture(params=["mysql"], ids=["mysql"])' in result.module.code
    assert '"only"' not in result.module.code


def test_a_conftest_never_moves_onto_a_fixtures_module_the_suite_already_has() -> None:
    # Overwriting a hand-written `fixtures.py` would lose code worth more than the fixtures moving
    # onto it, so the fixture gets no home and the caller refuses it.
    sources = {
        "api/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef payload():\n    ...\n",
        "api/fixtures.py": "SHARED = 1\n",
    }

    placed = layout.plan(
        {"k": _fixture_def("payload", "api")},
        symbols={("api/conftest.py", "payload"): "payload"},
        consumers={},
        source_of=sources.get,
    )

    assert placed.moves == {}
    assert placed.home("k") is None


def test_a_hazard_is_converted_rather_than_refused() -> None:
    # A hazard is what concurrency changes, not what syntax changes: `@velox.solo` is the answer and
    # the report names the tests needing it. Refusing a session fixture over an `os.environ` write
    # inside it would refuse everything downstream for something the rewrite does not fix.
    hazard = audit.Finding(code="VX402", message="writes the environment")

    assert not plan.refused(hazard)


def test_a_norecursedirs_pattern_is_dropped_rather_than_carried_as_a_name() -> None:
    # pytest matches `norecursedirs` as fnmatch patterns; velox's `ignore` compares directory names
    # exactly, so `.*` carried across verbatim would stop excluding anything.
    assert config._plain_names([".*", "build", "*.egg", "node_modules"]) == [
        "build",
        "node_modules",
    ]


# --- the type an injected parameter is written with ---------------------------------------------


@pytest.mark.parametrize(
    ("returns", "expected"),
    [
        ("Session", "Session"),
        ("Session | None", "Session | None"),
        ("dict[str, Widget]", "dict[str, Widget]"),
        ("Iterator[Session]", "Session"),
        ("Generator[Session, None, None]", "Session"),
        ("AsyncIterator[Session]", "Session"),
        ("AsyncGenerator[Session, None]", "Session"),
        ("Awaitable[Session]", "Session"),
        ("Coroutine[Any, Any, Session]", "Session"),
        ("typing.Iterator[Session]", "Session"),
        ("collections.abc.AsyncGenerator[Session, None]", "Session"),
        # Not one of the overloads: an `Iterable[X]` really is what the parameter receives, and a
        # wrapper spelled out of somebody else's module is somebody else's class.
        ("Iterable[Session]", "Iterable[Session]"),
        ("mymod.Iterator[Session]", "mymod.Iterator[Session]"),
        # Nothing to write: what a checker infers from these is what it infers from `Depends()`.
        ("Any", None),
        ("typing.Any", None),
        ("Iterator", None),
        (None, None),
    ],
)
def test_the_inferred_type_mirrors_what_the_fixture_decorator_unwraps(
    returns: str | None, expected: str | None
) -> None:
    assert annotate.infer(returns) == expected


def test_an_async_factory_unwraps_the_coroutine_its_annotation_does_not_name() -> None:
    # `async def f() -> Session` is a `Callable[..., Coroutine[Any, Any, Session]]`, which the
    # `Awaitable[T]` overload unwraps once — so a coroutine returning an iterator keeps it, and
    # only an async generator's annotation is the yielded type.
    assert annotate.infer("Session", is_async=True) == "Session"
    assert annotate.infer("Iterator[Session]", is_async=True) == "Iterator[Session]"
    assert annotate.infer("AsyncIterator[Session]", is_async=True, generator=True) == "Session"


def test_a_stringified_name_in_an_annotation_is_not_inferred_from() -> None:
    # A forward reference names something this cannot attribute to a module, and `Literal`'s
    # strings are values rather than names.
    assert annotate.infer('Session | "Later"') is None
    assert annotate.infer('Literal["a", "b"]') == "Literal['a', 'b']"


def test_the_names_an_annotation_needs_are_its_free_roots() -> None:
    assert annotate.free("dict[str, Widget] | None") == ("Widget",)
    assert annotate.free("pkg.mod.Thing") == ("pkg",)


def _annotation_of(
    sources: dict[str, str], fixture: model.FixtureDef, consumer: str
) -> tuple[annotate.Resolver, annotate.Typed | None]:
    """What `consumer` writes for an injection of `fixture`, laid out as the converter would."""
    container = layout.owning_container(fixture)
    assert container is not None
    placed = layout.plan(
        {fixture.key: fixture},
        symbols={(container, fixture.argname): fixture.argname},
        consumers={consumer: [fixture.key]},
        source_of=sources.get,
    )
    resolver = annotate.Resolver(sources, placed)
    return resolver, resolver.of(fixture, consumer, site=f"{consumer}::test_x")


_CONFTEST = """from support import Session


class Client:
    pass


@pytest.fixture
def session() -> Session:
    return Session()
"""

_TEST_IT = "def test_x(session):\n    ...\n"


def test_a_type_the_fixture_module_imported_is_imported_the_same_way_by_the_consumer() -> None:
    # The annotation is written in the fixture's module, so where that module got the name is
    # where the consuming module gets it: no re-export through the `fixtures.py` in between.
    sources = {"conftest.py": _CONFTEST, "test_it.py": _TEST_IT}
    fixture = _fixture_def("session", ".", returns="Session", file="conftest.py")

    _, typed = _annotation_of(sources, fixture, "test_it.py")

    assert typed == annotate.Typed(
        annotation="Session", imports=(annotate.TypeImport("support", "Session"),)
    )


def test_a_type_the_fixture_module_writes_itself_comes_from_where_that_module_lands() -> None:
    # A class written in a `conftest.py` travels with it to the `fixtures.py` the conversion moves
    # the conftest onto, and that is the module the type is importable out of.
    sources = {"conftest.py": _CONFTEST, "test_it.py": _TEST_IT}
    fixture = _fixture_def("session", ".", returns="Client", file="conftest.py")

    _, typed = _annotation_of(sources, fixture, "test_it.py")

    assert typed == annotate.Typed(
        annotation="Client", imports=(annotate.TypeImport("fixtures", "Client"),)
    )


def test_an_annotation_written_in_the_module_that_reads_it_needs_no_import() -> None:
    sources = {"conftest.py": _CONFTEST}
    fixture = _fixture_def("session", ".", returns="Session", file="conftest.py")

    _, typed = _annotation_of(sources, fixture, "fixtures.py")

    assert typed == annotate.Typed(annotation="Session", imports=())


def test_a_type_whose_name_the_consumer_already_binds_is_imported_under_an_alias() -> None:
    # Importing `Session` into a module that already binds it would silently rebind the module's
    # own name, which is the collision the fixture imports are aliased for too.
    sources = {
        "conftest.py": _CONFTEST,
        "test_it.py": f"Session = object()\n\n\n{_TEST_IT}",
    }
    fixture = _fixture_def("session", ".", returns="Session", file="conftest.py")

    _, typed = _annotation_of(sources, fixture, "test_it.py")

    assert typed == annotate.Typed(
        annotation="root_Session",
        imports=(annotate.TypeImport("support", "Session", "root_Session"),),
    )


def test_a_type_the_consumer_already_imports_the_same_way_needs_no_second_import() -> None:
    sources = {
        "conftest.py": _CONFTEST,
        "test_it.py": f"from support import Session\n\n\n{_TEST_IT}",
    }
    fixture = _fixture_def("session", ".", returns="Session", file="conftest.py")

    _, typed = _annotation_of(sources, fixture, "test_it.py")

    assert typed == annotate.Typed(annotation="Session", imports=())


def test_a_plain_import_the_consumer_already_binds_is_aliased_as_a_plain_import() -> None:
    # `import db` binds a module, so a collision has to be resolved with `import db as root_db`.
    # `from db import db as root_db` reads as the same rename and names something that is not there.
    conftest = "import db\n\n\ndef session() -> db.Session:\n    ...\n"
    sources = {"conftest.py": conftest, "test_it.py": f"db = object()\n\n\n{_TEST_IT}"}
    fixture = _fixture_def("session", ".", returns="db.Session", file="conftest.py")

    _, typed = _annotation_of(sources, fixture, "test_it.py")

    assert typed == annotate.Typed(
        annotation="root_db.Session", imports=(annotate.TypeImport("db", None, "root_db"),)
    )


def test_a_name_the_consumer_binds_only_under_type_checking_is_taken_all_the_same() -> None:
    # A name a suite uses only in annotations is written inside `if TYPE_CHECKING:`, which is
    # exactly where this pass writes too -- so missing it would rebind the module's own `Session`.
    sources = {
        "conftest.py": _CONFTEST,
        "test_it.py": f"if TYPE_CHECKING:\n    from other import Session\n\n\n{_TEST_IT}",
    }
    fixture = _fixture_def("session", ".", returns="Session", file="conftest.py")

    _, typed = _annotation_of(sources, fixture, "test_it.py")

    assert typed == annotate.Typed(
        annotation="root_Session",
        imports=(annotate.TypeImport("support", "Session", "root_Session"),),
    )


def test_the_import_the_consumer_already_writes_under_type_checking_is_not_written_twice() -> None:
    sources = {
        "conftest.py": _CONFTEST,
        "test_it.py": (
            f"if TYPE_CHECKING:\n    from support import Other, Session\n\n\n{_TEST_IT}"
        ),
    }
    fixture = _fixture_def("session", ".", returns="Session", file="conftest.py")

    _, typed = _annotation_of(sources, fixture, "test_it.py")

    assert typed == annotate.Typed(annotation="Session", imports=())


def test_an_aliased_typing_import_is_read_as_the_module_the_fixture_module_made_it() -> None:
    # `t.Iterator[Session]` is a shape to unwrap only if `t` is `typing`, and only the fixture's
    # own imports say so. Left unresolved it writes `t.Iterator[Session]` for a parameter every
    # checker reads as a `Session`, and `t.Any` for one this must leave alone entirely.
    imports = annotate.bindings("import typing as t\nimport collections.abc as ca\n")

    assert annotate.infer("t.Iterator[Session]", imports=imports) == "Session"
    assert annotate.infer("ca.AsyncIterator[Session]", imports=imports) == "Session"
    assert annotate.infer("t.Any", imports=imports) is None
    # Somebody else's `t` is not typing's, and `mymod.Generator` is their class.
    assert annotate.infer("t.Iterator[Session]") == "t.Iterator[Session]"
    assert annotate.infer("mymod.Generator[Session]") == "mymod.Generator[Session]"


def test_the_typed_suite_converts_with_nothing_refused(version: str) -> None:
    result = conversion_of(TYPED, version)

    assert result.plan.blocked_tests == frozenset()
    assert result.plan.blocked_fixtures == frozenset()
    assert result.refused == ()


def test_the_converted_typed_suite_passes_under_velox(version: str, tmp_path: Path) -> None:
    # The bar for this suite specifically: every type it writes is named through an import that
    # exists only for a type checker, so an annotation velox evaluated would not import at all.
    tree = tmp_path / TYPED
    converted(TYPED, version, tree)

    completed = subprocess.run(
        [*VELOX, "--serial", str(tree)], capture_output=True, text=True, check=False, cwd=tree
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_every_recoverable_shape_in_the_typed_suite_is_written_as_a_type(
    version: str, tmp_path: Path
) -> None:
    """One assertion per rule in `inference.infer`, read off a real conversion of a real suite.

    The rest of the corpus states no return types at all, so this is the only place the recovery
    half of the inference is exercised end to end rather than against a hand-built `FixtureDef`.
    """
    tree = tmp_path / TYPED
    converted(TYPED, version, tree)

    top = (tree / "test_top.py").read_text(encoding="utf-8")
    store = (tree / "store" / "test_store.py").read_text(encoding="utf-8")

    # `-> X`, and the consumer binds `Session` itself, so the import it is named through is aliased.
    session_site = "session: Annotated[root_Session, Depends(session)]"
    assert f"def test_session_states_its_type({session_site}):" in top
    assert "from support import Session as root_Session" in top
    # `-> Iterator[X]` is unwrapped, exactly as `@velox.fixture()`'s own overloads unwrap it.
    widget_site = "widget: Annotated[Widget, Depends(widget)]"
    assert f"def test_widget_states_what_it_yields({widget_site}):" in top
    # `-> t.Mapping[...]` is not one of the shapes to unwrap, and `t` is `typing` only because the
    # fixture's own module says so.
    assert "catalogue: Annotated[t.Mapping[str, Widget], Depends(catalogue)]" in top
    assert "import typing as t" in top
    # `-> t.AsyncIterator[X]` on an `async def` that yields.
    assert "channel: Annotated[list[str], Depends(channel)]" in top
    # A built-in's type comes from velox, since the suite's own sources say nothing about it.
    assert "tmp_path: Annotated[Path, Depends(velox.tmp_path)]" in top
    assert "capture: Annotated[velox.Capture, Depends(velox.capture)]" in top
    # A type named only under `TYPE_CHECKING` where it was written, and one written in a
    # `conftest.py`, which is importable only from the `fixtures.py` that conftest becomes.
    assert (
        "def test_report_names_a_type_checking_only_type("
        "report: Annotated[Report, Depends(report)]):" in store
    )
    assert "from store.records import Report" in store
    assert "from store.fixtures import Ledger" in store


def test_what_the_typed_suite_cannot_recover_is_written_bare_and_reported(
    version: str, tmp_path: Path
) -> None:
    tree = tmp_path / TYPED
    result = converted(TYPED, version, tree)

    top = (tree / "test_top.py").read_text(encoding="utf-8")

    untyped = "untyped: Annotated[Any, Depends(untyped)]"
    opaque = "opaque: Annotated[Any, Depends(opaque)]"

    assert f"def test_nothing_to_recover({untyped}, {opaque}):" in top

    degraded = {(row.fixture, row.reason) for row in result.plan.degraded}

    assert degraded == {
        ("untyped", "no return annotation"),
        ("opaque", "nothing to infer from `-> Any`"),
    }


def test_the_prefactor_worklist_names_the_fixtures_the_conversion_then_degrades(
    version: str,
) -> None:
    # The audit's worklist is what a user works through *before* converting, and the conversion
    # report is the same loss measured after. They read the same annotations through the same
    # `inference.infer`, so a suite cannot be told to annotate one set and then lose another.
    ground_truth = model.load(DUMPS / f"{TYPED}-pytest-{version}.json")
    audited = audit.run(ground_truth, root=CORPUS / TYPED)
    result = conversion_of(TYPED, version)

    assert {entry.argname for entry in audited.type_readiness.fixtures} == {
        row.fixture for row in result.plan.degraded
    }


def test_a_written_builtin_type_matches_what_velox_declares() -> None:
    """Every `BUILTIN_TYPES` row names the object velox's own fixture is declared to return.

    The table is written down because velox is not a dependency of this tool and the suite's
    sources say nothing about what `tmp_path` returns. This is what keeps it from drifting: it
    resolves both spellings to objects and compares those, so a rename or a retype in velox fails
    here rather than in somebody's converted suite.
    """
    # The one place this package reads velox, and a test rather than the tool: `velox-migrate`
    # converts a suite *for* velox and must keep working without it installed.
    import inspect

    import velox
    from velox._builtins import fixtures as declarations

    written_in = {"velox": velox, "Path": Path}
    declared_in = vars(declarations)
    spellings = annotate.bindings(inspect.getsource(declarations))

    for argname, (_, reference) in plan.BUILTINS.items():
        declared = getattr(velox, reference.removeprefix("velox.")).func.__annotations__["return"]
        unwrapped = annotate.infer(declared, imports=spellings)
        written, _ = annotate.BUILTIN_TYPES[argname]

        assert unwrapped is not None, f"{reference} declares nothing to write"
        assert eval(written, written_in) is eval(unwrapped, declared_in), argname


def test_every_builtin_with_a_velox_counterpart_has_a_type_to_write() -> None:
    assert set(annotate.BUILTIN_TYPES) == set(plan.BUILTINS)


def _builtin_work(*injections: plan.Injection) -> plan.FileWork:
    return plan.FileWork(
        path="test_it.py",
        target="test_it.py",
        tests=(plan.TestWork(qualname="test_x", injections=injections),),
    )


_CAPSYS = plan.Injection(
    was="capsys",
    param="capture",
    reference="velox.capture",
    annotation="velox.Capture",
    retypes=True,
)
_TMP_PATH = plan.Injection(
    was="tmp_path",
    param="tmp_path",
    reference="velox.tmp_path",
    annotation="Path",
    needs=(annotate.TypeImport("pathlib", "Path"),),
)


def test_a_builtin_is_written_with_velox_s_type_and_not_the_one_pytest_gave_it() -> None:
    # `capsys` becomes a `velox.Capture`, an object with different methods -- so unlike a fixture
    # the suite wrote, the annotation the author put on it is no longer true and is replaced.
    source = "def test_x(capsys: CaptureFixture[str], tmp_path):\n    ...\n"

    result = wiring.apply(cst.parse_module(source), _builtin_work(_CAPSYS, _TMP_PATH))

    assert "capture: Annotated[velox.Capture, Depends(velox.capture)]" in result.module.code
    assert "tmp_path: Annotated[Path, Depends(velox.tmp_path)]" in result.module.code
    assert "CaptureFixture" not in result.module.code
    assert "if TYPE_CHECKING:\n    from pathlib import Path\n" in result.module.code


def test_the_import_a_replaced_annotation_was_named_through_goes_with_it() -> None:
    # Nothing else in the file reads `CaptureFixture` once `capsys` stops being one, and an import
    # left behind would keep the converted module importing pytest to satisfy no reader.
    source = (
        "from _pytest.capture import CaptureFixture\n\n\n"
        "def test_x(capsys: CaptureFixture[str]):\n    ...\n"
    )

    result = wiring.apply(cst.parse_module(source), _builtin_work(_CAPSYS))

    assert "CaptureFixture" not in result.module.code
    assert "_pytest" not in result.module.code


def test_an_import_a_replaced_annotation_shared_with_another_reader_stays() -> None:
    source = (
        "from _pytest.capture import CaptureFixture\n\n\n"
        "def helper(other: CaptureFixture[str]) -> None: ...\n\n\n"
        "def test_x(capsys: CaptureFixture[str]):\n    ...\n"
    )

    result = wiring.apply(cst.parse_module(source), _builtin_work(_CAPSYS))

    assert "from _pytest.capture import CaptureFixture" in result.module.code


def test_a_builtin_velox_hands_back_unchanged_keeps_the_annotation_its_author_wrote() -> None:
    # velox's `tmp_path` is the same `pathlib.Path` pytest's was, so the author already answered
    # this question correctly and rewriting them would orphan their `import pathlib` for nothing.
    source = "import pathlib\n\n\ndef test_x(tmp_path: pathlib.Path):\n    ...\n"

    result = wiring.apply(cst.parse_module(source), _builtin_work(_TMP_PATH))

    assert "tmp_path: Annotated[pathlib.Path, Depends(velox.tmp_path)]" in result.module.code
    assert "import pathlib" in result.module.code
    assert "TYPE_CHECKING" not in result.module.code


def test_only_the_builtins_velox_changes_the_object_of_replace_an_annotation() -> None:
    assert {"tmp_path"} == annotate.SAME_AS_PYTEST
    assert set(plan.BUILTINS) > annotate.SAME_AS_PYTEST


def test_a_builtin_type_the_consumer_already_binds_is_imported_under_an_alias() -> None:
    resolver = annotate.Resolver(
        {"test_it.py": "Path = object()\n\n\ndef test_x(tmp_path):\n    ...\n"},
        layout.Layout(homes={}, moves={}, imports={}),
    )

    typed = resolver.builtin("tmp_path", "test_it.py")

    assert typed == annotate.Typed(
        annotation="velox_Path", imports=(annotate.TypeImport("pathlib", "Path", "velox_Path"),)
    )


def test_a_builtin_velox_has_no_counterpart_for_is_no_worklist_row() -> None:
    # There is no injection to lose a type at, so there is nothing for a user to go and annotate.
    resolver = annotate.Resolver({}, layout.Layout(homes={}, moves={}, imports={}))

    assert resolver.builtin("monkeypatch", "test_it.py") is None
    assert resolver.degraded == ()


def test_an_unaliased_dotted_typing_import_is_read_through_the_head_it_binds() -> None:
    imports = annotate.bindings("import collections.abc\n")

    assert annotate.infer("collections.abc.Iterator[Session]", imports=imports) == "Session"


def test_a_relative_import_in_the_fixture_module_is_spelled_absolutely_for_the_consumer() -> None:
    # A relative import reaches inside a package, so the absolute path to what it names is one any
    # module in the suite can use — which is how the conversion spells every import it writes.
    conftest = "from .support import Session\n\n\ndef session() -> Session:\n    ...\n"
    sources = {"api/conftest.py": conftest, "test_it.py": _TEST_IT}
    fixture = _fixture_def("session", "api", returns="Session", file="api/conftest.py")

    _, typed = _annotation_of(sources, fixture, "test_it.py")

    assert typed == annotate.Typed(
        annotation="Session", imports=(annotate.TypeImport("api.support", "Session"),)
    )


def test_a_name_no_import_can_attribute_to_a_module_falls_back_and_is_reported() -> None:
    # A name the fixture's module does not bind at its top level — imported inside the body, or
    # built somewhere this cannot see — is one no import here could supply, and a fallback is a
    # row rather than a guess.
    sources = {
        "conftest.py": "def session() -> Session:\n    from support import Session\n",
        "test_it.py": _TEST_IT,
    }
    fixture = _fixture_def("session", ".", returns="Session", file="conftest.py")

    resolver, typed = _annotation_of(sources, fixture, "test_it.py")

    assert typed is None
    assert [item.reason for item in resolver.degraded] == [
        "`Session` is not attributable to an importable module"
    ]


def test_a_fixture_with_no_return_annotation_is_the_worklist_row_it_earns() -> None:
    sources = {"conftest.py": "def session():\n    ...\n", "test_it.py": _TEST_IT}
    fixture = _fixture_def("session", ".", file="conftest.py")

    resolver, typed = _annotation_of(sources, fixture, "test_it.py")

    assert typed is None
    assert resolver.degraded == (
        annotate.Degraded(
            fixture="session",
            defined="conftest.py:1",
            site="test_it.py::test_x",
            reason="no return annotation",
        ),
    )


def test_a_type_written_in_a_conftest_the_conversion_does_not_move_falls_back() -> None:
    # Nothing can import a `conftest.py`, so a class written in one that stays put has no module a
    # consumer could name it out of.
    sources = {"conftest.py": _CONFTEST}
    fixture = _fixture_def("session", ".", returns="Client", file="conftest.py")
    resolver = annotate.Resolver(sources, layout.Layout(homes={}, moves={}, imports={}))

    typed = resolver.of(fixture, "test_it.py", site="test_it.py::test_x")

    assert typed is None
    assert resolver.degraded[0].reason == "`Client` is not attributable to an importable module"


def _typed_work(annotation: str | None) -> plan.FileWork:
    """One test injecting a `Session`-typed fixture, as the plan hands it to the wiring swap."""
    return plan.FileWork(
        path=STANDALONE,
        target=STANDALONE,
        tests=(
            plan.TestWork(
                qualname="test_x",
                injections=(
                    plan.Injection(
                        "session",
                        "session",
                        "session",
                        annotation=annotation,
                        needs=(annotate.TypeImport("support", "Session"),),
                    ),
                ),
            ),
        ),
    )


def test_an_inferred_type_is_written_with_a_type_checking_import_and_the_future_import() -> None:
    # The annotation is evaluated when the `def` is read, so a `TYPE_CHECKING`-only import is only
    # safe under `from __future__ import annotations` — which is why the two are written together.
    source = "def test_x(session):\n    assert session\n"

    result = wiring.apply(cst.parse_module(source), _typed_work("Session"))

    assert "from __future__ import annotations" in result.module.code
    assert "if TYPE_CHECKING:\n    from support import Session\n" in result.module.code
    assert "def test_x(session: Annotated[Session, Depends(session)]):" in result.module.code


def test_a_parameter_the_source_already_annotated_keeps_its_own_type_and_needs_no_import() -> None:
    source = "def test_x(session: Mine):\n    assert session\n"

    result = wiring.apply(cst.parse_module(source), _typed_work("Session"))

    assert "def test_x(session: Annotated[Mine, Depends(session)]):" in result.module.code
    assert "TYPE_CHECKING" not in result.module.code


def test_an_injection_with_no_type_is_written_exactly_as_it_was_before() -> None:
    source = "def test_x(session):\n    assert session\n"

    result = wiring.apply(cst.parse_module(source), _typed_work(None))

    assert "def test_x(session: Annotated[Any, Depends(session)]):" in result.module.code
    assert "TYPE_CHECKING" not in result.module.code
    assert "from __future__ import annotations" not in result.module.code


def test_a_signature_the_rewrite_backs_out_of_leaves_no_type_checking_import_behind() -> None:
    source = "def test_x(capsys, session):\n    assert capsys.readouterr()[0] and session\n"
    work = plan.FileWork(
        path=STANDALONE,
        target=STANDALONE,
        tests=(
            plan.TestWork(
                qualname="test_x",
                injections=(
                    plan.Injection("capsys", "capture", "velox.capture"),
                    *_typed_work("Session").tests[0].injections,
                ),
            ),
        ),
    )

    result = wiring.apply(cst.parse_module(source), work)

    assert result.refused == (("test_x", "VX202"),)
    assert result.module.code == source


def test_every_injection_that_lost_its_type_is_named_in_the_conversion_report(version: str) -> None:
    # No fixture in the corpus annotates its return, so the whole suite is the worklist — which is
    # what the report is for: the fixtures to annotate, worst first, and the sites each one costs.
    result = conversion_of(FIXTURES, version)

    text = conversion_report.plan(result)

    assert "degrade to Any: 5 fixture(s), 8 injection site(s)" in text
    assert "  settings (conftest.py:21): no return annotation — 3 site(s)" in text
    assert "    test_top.py::TestGroup.test_method" in text
    assert {item.reason for item in result.plan.degraded} == {"no return annotation"}


def test_two_injections_of_one_fixture_into_one_module_agree_about_the_import() -> None:
    # The first injection claims `Session` in the consuming module; the second must read that as
    # its own claim rather than as a collision to alias around.
    sources = {"conftest.py": _CONFTEST, "test_it.py": _TEST_IT}
    fixture = _fixture_def("session", ".", returns="Session", file="conftest.py")
    container = layout.owning_container(fixture)
    assert container is not None
    placed = layout.plan(
        {fixture.key: fixture},
        symbols={(container, fixture.argname): fixture.argname},
        consumers={"test_it.py": [fixture.key]},
        source_of=sources.get,
    )
    resolver = annotate.Resolver(sources, placed)

    first = resolver.of(fixture, "test_it.py", site="test_it.py::test_x")
    second = resolver.of(fixture, "test_it.py", site="test_it.py::test_y")

    assert first == second
    assert second == annotate.Typed(
        annotation="Session", imports=(annotate.TypeImport("support", "Session"),)
    )
