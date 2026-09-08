"""Fixtures a container declares for the tests inside it, in place of pytest's implicit ones.

An autouse fixture and a `usefixtures` mark say the same thing in two spellings — these tests get
this fixture without naming it — and voci says it once, as `voci.use(...)` on the module or the
package `__init__.py` covering the same tests. So the translation is a placement question, and the
dump answers it: pytest keys autouse fixtures by the node they became visible at, which is the
directory, or the module, whose tests they reached.

Two things follow from voci reading a package declaration by walking up from the test file. The
`__init__.py` files between a declaring directory and each test under it all have to exist, or the
walk stops before it gets there, so this module names them as well. And a declaration is an
ordinary reference to an imported object, which means a container declaring a fixture written in
its own body has to say so after that body — a `voci.use` above the `def` it names would read a
name nothing has bound yet.
"""

from __future__ import annotations

from collections.abc import Container, Iterator, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

import libcst as cst

from voci_migrate.audit.wiring import in_suite
from voci_migrate.model import GroundTruth, Item

__all__ = ["Declaration", "apply", "container_for", "packages", "plan"]

INIT = "__init__.py"

USE = "use"
VOCI = "voci"

# The nodes that cover every test there is: pytest's session, spelled empty, and the rootdir.
_SESSION = ("", ".", "/")


@dataclass(frozen=True, slots=True)
class Declaration:
    """One `voci.use(...)` line: where it goes, and the fixtures it names.

    `container` is the module that declares — a package `__init__.py`, or a test module for a
    fixture that only ever reached its own file. `keys` are fixture keys, in the order pytest set
    the fixtures up in.
    """

    container: str
    keys: tuple[str, ...]

    @property
    def directory(self) -> str | None:
        """The directory this declaration covers, or `None` for a single module's own."""
        if PurePosixPath(self.container).name != INIT:
            return None
        return str(PurePosixPath(self.container).parent) if "/" in self.container else ""


def container_for(node: str) -> str | None:
    """The module a fixture visible at `node` is declared in, or `None` where none can be.

    A class's node has no counterpart: `voci.use(...)` covers a module or a package, and a fixture
    written in a class body binds no name a declaration could import anyway.
    """
    if node in _SESSION:
        return INIT
    if "::" in node:
        return None
    if node.endswith(".py"):
        return node
    return f"{node}/{INIT}"


def plan(
    ground_truth: GroundTruth, *, available: Container[str], blocked: Container[str] = ()
) -> tuple[Declaration, ...]:
    """Every declaration this suite needs, for the tests it converts and the fixtures it has.

    A fixture the conversion is leaving as pytest wrote it is left out: the tests it reaches keep
    their pytest source and are reported as refused, and a declaration naming an object nobody
    built would break the module carrying it as well. So are the declarations only a refused test
    asked for — that test still carries the mark it was written with, and giving the module's other
    tests a fixture on its behalf would be a widening nobody gets anything from.
    """
    keys: dict[str, list[str]] = {}
    for container, key in _wanted(_converting(ground_truth, blocked), ground_truth):
        if key not in available:
            continue
        found = keys.setdefault(container, [])
        if key not in found:
            found.append(key)
    return tuple(
        Declaration(container=container, keys=tuple(found))
        for container, found in sorted(keys.items())
    )


def packages(
    ground_truth: GroundTruth,
    declarations: Sequence[Declaration],
    *,
    blocked: Container[str] = (),
) -> tuple[str, ...]:
    """The `__init__.py` files that have to exist for `declarations` to be read.

    voci reads a package's declarations by walking up from the test file and stopping at the first
    directory without an `__init__.py`, so every directory between a declaring one and a test under
    it needs one — including the declaring directory itself, which is where its own declaration is
    written.
    """
    converting = _converting(ground_truth, blocked)
    wanted: set[str] = set()
    for declaration in declarations:
        directory = declaration.directory
        if directory is None:
            continue
        for path in _test_files(converting, directory):
            wanted |= {_init_in(part) for part in _between(directory, path)}
    return tuple(sorted(wanted))


def apply(module: cst.Module, references: Sequence[str]) -> cst.Module:
    """`module` with a `voci.use(...)` naming every one of `references` it does not name yet.

    Idempotent by reading what the module already declares rather than by tracking what a previous
    run wrote, so a hand-written declaration counts the same as a generated one and a second
    conversion adds nothing.
    """
    missing = [name for name in references if name not in _declared(module)]
    if not missing:
        return module
    body = list(module.body)
    index = _where(body, missing)
    body.insert(index, _statement(missing, after=body[index - 1] if index else None))
    return module.with_changes(body=body)


def _wanted(converting: Sequence[Item], ground_truth: GroundTruth) -> Iterator[tuple[str, str]]:
    """Every (container, fixture key) this suite's implicit wiring asks for."""
    for node, names in sorted(ground_truth.autouse_by_node.items()):
        container = container_for(node)
        if container is None:
            continue
        covered = _covered(converting, node)
        for name in names:
            key = _resolved(covered, name)
            if key is not None:
                yield container, key

    for item in converting:
        if item.path is None:
            continue
        for name in item.usefixtures:
            key = _resolved((item,), name)
            if key is not None:
                yield item.path, key


def _converting(ground_truth: GroundTruth, blocked: Container[str]) -> tuple[Item, ...]:
    """The tests this conversion rewrites, which are the ones a declaration is written for."""
    return tuple(item for item in ground_truth.items if item.nodeid not in blocked)


def _covered(converting: Sequence[Item], node: str) -> tuple[Item, ...]:
    """The tests `node` reaches: a directory's, a module's, or the whole session's."""
    if node in _SESSION:
        return tuple(converting)
    return tuple(
        item
        for item in converting
        if item.nodeid == node or item.nodeid.startswith((f"{node}/", f"{node}::"))
    )


def _resolved(covered: Sequence[Item], name: str) -> str | None:
    """The one definition `name` means for `covered`, or `None` where it does not mean one.

    Asked of the tests the node reaches rather than of the fixture table, because that is what
    pytest's own answer is: an ini-level `usefixtures` names a fixture that is autouse nowhere, and
    a name two of these tests resolve differently is an override, which the conversion refuses
    where the override is written rather than declaring one of the two here.
    """
    found = {
        fixture.key: fixture for item in covered if (fixture := item.resolve(name)) is not None
    }
    if len(found) != 1:
        return None
    ((key, fixture),) = found.items()
    return key if in_suite(fixture) else None


def _test_files(converting: Sequence[Item], directory: str) -> Iterator[str]:
    """The suite files holding a test under `directory`, deduplicated."""
    seen: set[str] = set()
    for item in converting:
        path = item.path
        if path is None or path in seen:
            continue
        if directory and not path.startswith(f"{directory}/"):
            continue
        seen.add(path)
        yield path


def _between(directory: str, path: str) -> Iterator[str]:
    """Every directory from `directory` down to the one holding `path`, inclusive."""
    parts = PurePosixPath(path).parent
    walked = PurePosixPath(directory) if directory else PurePosixPath(".")
    yield directory
    for part in parts.parts[len(walked.parts) if directory else 0 :]:
        walked = walked / part
        yield str(walked)


def _init_in(directory: str) -> str:
    return f"{directory}/{INIT}" if directory and directory != "." else INIT


def _declared(module: cst.Module) -> frozenset[str]:
    """Every name a `voci.use(...)` at the top of `module` already declares."""
    found: set[str] = set()
    for statement in module.body:
        if not isinstance(statement, cst.SimpleStatementLine):
            continue
        for small in statement.body:
            if isinstance(small, cst.Expr) and _is_use(small.value):
                call = small.value
                assert isinstance(call, cst.Call)
                found |= {
                    argument.value.value
                    for argument in call.args
                    if isinstance(argument.value, cst.Name)
                }
    return frozenset(found)


def _is_use(expression: cst.BaseExpression) -> bool:
    """Whether `expression` is a `voci.use(...)` call, spelled as this module writes one."""
    match expression:
        case cst.Call(func=cst.Attribute(value=cst.Name(value="voci"), attr=cst.Name(value="use"))):
            return True
        case _:
            return False


def _where(body: Sequence[cst.BaseStatement], references: Sequence[str]) -> int:
    """Where in `body` the declaration goes.

    Straight after the imports, which is where a reader looks for what a module pulls in — unless
    the declaration names something the module defines itself, in which case it goes last, since
    the name is not bound until the `def` it comes from has been read. A module that already
    declares gets this one underneath, because declarations apply in the order they are written.
    A module with neither imports nor declarations still keeps its docstring first, which is the
    one statement whose position is part of what it means.
    """
    if _defines_any(body, references):
        return len(body)
    last = 1 if body and _is_docstring(body[0]) else 0
    for index, statement in enumerate(body):
        if not isinstance(statement, cst.SimpleStatementLine):
            continue
        if any(isinstance(small, cst.Import | cst.ImportFrom) for small in statement.body) or any(
            isinstance(small, cst.Expr) and _is_use(small.value) for small in statement.body
        ):
            last = index + 1
    return last


def _is_docstring(statement: cst.BaseStatement) -> bool:
    match statement:
        case cst.SimpleStatementLine(body=[cst.Expr(value=cst.SimpleString()), *_]):
            return True
        case _:
            return False


def _defines_any(body: Sequence[cst.BaseStatement], references: Sequence[str]) -> bool:
    wanted = set(references)
    return any(
        isinstance(statement, cst.FunctionDef | cst.ClassDef) and statement.name.value in wanted
        for statement in body
    )


def _statement(
    references: Sequence[str], *, after: cst.BaseStatement | None
) -> cst.SimpleStatementLine:
    """`voci.use(...)`, spaced from what it follows the way a formatter would space it."""
    blanks = 2 if isinstance(after, cst.FunctionDef | cst.ClassDef) else 1
    return cst.SimpleStatementLine(
        body=[
            cst.Expr(
                cst.Call(
                    func=cst.Attribute(value=cst.Name(VOCI), attr=cst.Name(USE)),
                    args=[
                        cst.Arg(value=cst.Name(name), comma=cst.MaybeSentinel.DEFAULT)
                        for name in references
                    ],
                )
            )
        ],
        leading_lines=[cst.EmptyLine() for _ in range(blanks)],
    )
