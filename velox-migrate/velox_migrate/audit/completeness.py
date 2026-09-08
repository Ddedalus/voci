"""What the dump names that a plain read of the sources cannot find.

`sources.scan` reports on a construct only where it locates the node that carries it, and the
fixture-census helpers in `wiring.py` classify a fixture only once it is on the graph a test's
closure reaches — both are occurrence-based, so a fixture, test or class the dump says pytest
resolved, whose defining code no walk here can find, produces no finding at all rather than a
finding that says so. That is indistinguishable from a construct genuinely clean, which is how
marshmallow's audit once read four unrecognised constructs as *converts untouched*.

This closes that gap the only way that scales to a construct the matrix has no row for yet: not by
recognising more shapes, but by cross-checking the dump's own census — pytest's word that a fixture
or test exists — against a plain `ast` walk of the same file, independent of `sources.py`'s LibCST
pass so a shape one misses is not guaranteed to fool the other too. A name the dump resolved that
this walk cannot find as a `def` or `class` in its file is exactly "nothing looked at this": built
dynamically, hidden behind a decorator that does not preserve it, or nested somewhere pytest's own
collection descends into but this walk does not.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from velox_migrate.audit.findings import Site, Unclassified, unclassified_ordered
from velox_migrate.audit.reach import Reach
from velox_migrate.audit.wiring import in_suite
from velox_migrate.model import GroundTruth


@dataclass(frozen=True, slots=True)
class Defined:
    """Every function and class one module's source defines, by qualified name.

    Only class bodies are entered looking for more of either: pytest never collects a test or a
    fixture nested inside a function, so a walk that also descended into one would just be reading
    code no dump entry can ever point at.
    """

    functions: frozenset[str]
    async_functions: frozenset[str]
    classes: frozenset[str]


def parse(root: Path, files: list[str]) -> dict[str, Defined]:
    """`Defined` for every file in `files` that parses.

    A file that does not is left out rather than raising: `sources.scan` already names it in
    `unparsed`, and this draws no conclusion from a file it could not read either.
    """
    found: dict[str, Defined] = {}
    for file in files:
        try:
            tree = ast.parse(Path(root, file).read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        found[file] = _walk(tree)
    return found


def _walk(tree: ast.Module) -> Defined:
    functions: set[str] = set()
    async_functions: set[str] = set()
    classes: set[str] = set()

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = f"{prefix}{node.name}"
                functions.add(qualname)
                if isinstance(node, ast.AsyncFunctionDef):
                    async_functions.add(qualname)
            elif isinstance(node, ast.ClassDef):
                qualname = f"{prefix}{node.name}"
                classes.add(qualname)
                visit(node.body, f"{qualname}.")

    visit(tree.body, "")
    return Defined(frozenset(functions), frozenset(async_functions), frozenset(classes))


def missing(
    ground_truth: GroundTruth, reach: Reach, defined: dict[str, Defined]
) -> tuple[Unclassified, ...]:
    """Every fixture, test and class the dump names whose defining node `defined` never found.

    A `__qualname__` carrying `<locals>` names a `def` built inside another function's body at
    call time — a factory-of-fixtures pattern, say — which no lexical walk of a module's top level
    and class bodies can ever resolve to a line. That is a real blind spot, already the kind
    `matrix.CONSTRUCTS`'s `detected=False` rows exist for, not an omission this can report on.
    """
    found = _missing_fixtures(ground_truth, reach, defined)
    missing_classes, class_found = _missing_classes(ground_truth, reach, defined)
    found += class_found
    found += _missing_tests(ground_truth, defined, missing_classes)
    return unclassified_ordered(found)


def _missing_fixtures(
    ground_truth: GroundTruth, reach: Reach, defined: dict[str, Defined]
) -> list[Unclassified]:
    found: list[Unclassified] = []
    for fixture in ground_truth.fixture_defs.values():
        file, qualname = fixture.func.file, fixture.func.qualname
        if (
            not in_suite(fixture)
            or file is None
            or qualname is None
            or file not in defined
            or _dynamic(qualname)
        ):
            continue
        if qualname not in defined[file].functions:
            found.append(
                Unclassified(
                    kind="fixture",
                    name=fixture.argname,
                    site=Site(file, fixture.func.lineno, qualname),
                    tests=reach.tests_at(file, qualname),
                )
            )
    return found


def _missing_classes(
    ground_truth: GroundTruth, reach: Reach, defined: dict[str, Defined]
) -> tuple[set[tuple[str, str]], list[Unclassified]]:
    missing_classes: set[tuple[str, str]] = set()
    found: list[Unclassified] = []
    for item in ground_truth.items:
        if item.cls is None or item.path is None or item.path not in defined:
            continue
        key = (item.path, item.cls)
        if key in missing_classes or item.cls in defined[item.path].classes:
            continue
        missing_classes.add(key)
        found.append(
            Unclassified(
                kind="class",
                name=item.cls,
                site=Site(item.path, None, item.cls),
                tests=reach.tests_at(item.path, item.cls),
            )
        )
    return missing_classes, found


def _missing_tests(
    ground_truth: GroundTruth, defined: dict[str, Defined], missing_classes: set[tuple[str, str]]
) -> list[Unclassified]:
    found: list[Unclassified] = []
    for item in ground_truth.items:
        if item.path is None or item.path not in defined:
            continue
        # A class this walk never found already reports every test under it; a method the walk
        # cannot resolve because its class isn't there is the same gap, not a second one.
        if item.cls is not None and (item.path, item.cls) in missing_classes:
            continue
        qualname = f"{item.cls}.{item.originalname}" if item.cls else item.originalname
        if qualname not in defined[item.path].functions:
            found.append(
                Unclassified(
                    kind="test",
                    name=item.originalname,
                    site=Site(item.path, item.lineno, qualname),
                    tests=(item.nodeid,),
                )
            )
    return found


def _dynamic(qualname: str) -> bool:
    """Whether `__qualname__` names a `def` nested inside a function rather than a class."""
    return "<locals>" in qualname
