"""Tests for velox_migrate.convert.declarations: where a `velox.use(...)` goes, and what it names.

The corpus suite is the subject wherever a placement question is really a question about pytest's
own visibility, since the dump is the only honest source for that; the writer itself is tested on
small modules, where the property that matters is that a second run adds nothing.
"""

from __future__ import annotations

from pathlib import Path

import libcst as cst

import pytest
from velox_migrate import audit, convert, model
from velox_migrate.convert import declarations

CORPUS = Path(__file__).resolve().parents[1] / "corpus"
DUMPS = CORPUS / "dumps"
PYTEST_VERSIONS = ["8.4", "9.1"]

DECLARATIONS = "declarations_showcase"
FIXTURES = "fixtures_showcase"


@pytest.fixture(params=PYTEST_VERSIONS, ids=[f"pytest{v}" for v in PYTEST_VERSIONS])
def version(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def conversion_of(suite: str, version: str) -> convert.Conversion:
    ground_truth = model.load(DUMPS / f"{suite}-pytest-{version}.json")
    root = CORPUS / suite
    return convert.run(audit.run(ground_truth, root=root), ground_truth, root=root)


def declared_in(conversion: convert.Conversion, container: str) -> tuple[str, ...]:
    """The names the container declares, as the conversion writes them."""
    work = conversion.plan.work.get(container)
    return work.declares if work is not None else ()


# --- placement --------------------------------------------------------------------------------


def test_a_rootdir_autouse_fixture_is_declared_in_a_root_package(version: str) -> None:
    conversion = conversion_of(DECLARATIONS, version)

    assert "root_stamp" in declared_in(conversion, "__init__.py")


def test_a_directory_autouse_fixture_is_declared_in_that_directory(version: str) -> None:
    conversion = conversion_of(DECLARATIONS, version)

    assert declared_in(conversion, "api/__init__.py") == ("api_seed",)


def test_a_modules_own_autouse_fixture_is_declared_in_that_module(version: str) -> None:
    conversion = conversion_of(DECLARATIONS, version)

    assert "top_stamp" in declared_in(conversion, "test_top.py")


def test_the_ini_files_usefixtures_is_declared_like_an_autouse_fixture(version: str) -> None:
    # `audited` is autouse nowhere: pytest registers an ini-level `usefixtures` at the session
    # node, which the extractor folds into the rootdir, and the fixture it names is an ordinary
    # one. Reading the name against the tests the node covers is what finds it.
    conversion = conversion_of(DECLARATIONS, version)

    assert "audited" in declared_in(conversion, "__init__.py")


def test_a_declaration_keeps_the_order_pytest_set_the_fixtures_up_in(version: str) -> None:
    conversion = conversion_of(DECLARATIONS, version)
    ground_truth = model.load(DUMPS / f"{DECLARATIONS}-pytest-{version}.json")

    assert declared_in(conversion, "__init__.py") == ground_truth.autouse_by_node["."]


def test_a_class_node_declares_nothing() -> None:
    # `velox.use(...)` covers a module or a package, and a fixture written in a class body binds no
    # name a declaration could import in the first place.
    assert declarations.container_for("tests/test_it.py::TestGroup") is None
    assert declarations.container_for("tests/test_it.py") == "tests/test_it.py"
    assert declarations.container_for("tests/integration") == "tests/integration/__init__.py"
    assert declarations.container_for(".") == "__init__.py"


# --- the package chain ------------------------------------------------------------------------


def test_every_directory_between_a_declaration_and_a_test_becomes_a_package(
    version: str,
) -> None:
    # velox walks up from the test file and stops at the first directory that is not a package, so
    # a missing `api/__init__.py` would leave the rootdir's declaration unread under `api/`.
    conversion = conversion_of(DECLARATIONS, version)

    assert conversion.plan.packages == ("__init__.py", "api/__init__.py")


def test_the_packages_a_conversion_creates_are_written_empty(version: str, tmp_path: Path) -> None:
    conversion = conversion_of(DECLARATIONS, version)
    created = {edit.path: edit for edit in conversion.edits.changes if edit.kind == "create"}

    assert created["api/__init__.py"].new_text is not None
    assert "velox.use(api_seed)" in created["api/__init__.py"].new_text


# --- what is left out -------------------------------------------------------------------------


def test_a_fixture_the_conversion_refuses_is_not_declared(version: str) -> None:
    # `integ_autouse` converts, but every test under `integration/` resolves the refused override
    # chain and keeps its pytest source -- so there is nobody left for the declaration to serve.
    conversion = conversion_of(FIXTURES, version)

    assert "integration/__init__.py" not in {d.container for d in conversion.plan.declarations}


def test_a_usefixtures_only_a_refused_test_asked_for_is_not_declared(version: str) -> None:
    # Declaring it would widen the fixture onto the module's other tests on behalf of a test that
    # is not being converted at all.
    conversion = conversion_of(FIXTURES, version)

    assert declared_in(conversion, "test_top.py") == ()


# --- the writer -------------------------------------------------------------------------------


def test_a_declaration_lands_after_the_imports() -> None:
    source = '"""Doc."""\n\nimport velox\nfrom fixtures import db\n\n\ndef test_x():\n    pass\n'

    written = declarations.apply(cst.parse_module(source), ["db"])

    assert written.code == (
        '"""Doc."""\n\nimport velox\nfrom fixtures import db\n\nvelox.use(db)\n\n\n'
        "def test_x():\n    pass\n"
    )


def test_a_declaration_naming_the_modules_own_fixture_lands_after_it() -> None:
    # The declaration is an ordinary reference to the object, so it cannot be read before the `def`
    # that binds it has been.
    module = cst.parse_module("import velox\n\n\n@velox.fixture()\ndef seed():\n    pass\n")

    written = declarations.apply(module, ["seed"])

    assert written.code.endswith("\n\n\nvelox.use(seed)\n")


def test_a_second_pass_declares_nothing_twice() -> None:
    module = cst.parse_module("import velox\nfrom fixtures import db\n")

    once = declarations.apply(module, ["db"])
    twice = declarations.apply(once, ["db"])

    assert once.code.count("velox.use") == 1
    assert twice.code == once.code


def test_a_declaration_the_module_already_makes_is_left_alone() -> None:
    # And the one it does not make goes underneath it, since declarations apply in source order.
    source = "import velox\nfrom fixtures import db, seed\n\nvelox.use(db)\n"

    written = declarations.apply(cst.parse_module(source), ["db", "seed"])

    assert written.code == (
        "import velox\nfrom fixtures import db, seed\n\nvelox.use(db)\n\nvelox.use(seed)\n"
    )
