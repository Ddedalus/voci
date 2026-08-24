"""The conversion: a pytest suite rewritten as a velox one, deterministically.

`convert` is the one stage with no verification oracle behind it — the suite is green under pytest
before it runs and green under velox only after `verify` says so — which is why it is mechanical,
why no model reasons about a fixture body here, and why running it twice changes nothing the second
time. Everything that needs a judgement is refused and named in the source instead.

The order per file is fixed and matters. A fixture a test class wrote is lifted out to the module
level first, and a specialized copy is written into its module next, both in the pytest spelling
they came in, so everything below translates them exactly as it translates the definitions that
were already there. Then each enabled rule, in code order, each
matching only a pytest source form its own rewrite eliminates — and each reading the parameter
names the suite wrote, which is why they run before the swap rather than after it: a body saying
`capsys.readouterr()` is recognized by the `capsys` its enclosing signature still declares. Then
the wiring swap, which owns signatures, fixture decorators and imports; then the `velox.use(...)`
declarations, which name what the swap has just imported; then `@velox.solo` on the tests that take
the run to themselves; then the markers, which are comments and disturb nothing.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import libcst as cst

from velox_migrate import matrix
from velox_migrate.audit import Audit
from velox_migrate.convert import (
    config,
    declarations,
    layout,
    lift,
    markers,
    plan,
    rules,
    solo,
    wiring,
)
from velox_migrate.convert.edits import Edit, EditSet
from velox_migrate.convert.plan import DEFERRED, FileWork, Plan
from velox_migrate.model import GroundTruth

__all__ = [
    "DEFERRED",
    "Conversion",
    "Edit",
    "EditSet",
    "Plan",
    "run",
]


@dataclass(frozen=True, slots=True)
class Conversion:
    """Everything one conversion of a suite would do, before anything is written.

    `refused` names the definitions the rewrite backed out of, each against the support-matrix code
    that says why; `unreadable` names the files the dump described that the tree does not hold,
    which is also what makes a second conversion of an already-converted tree a no-op.
    """

    plan: Plan
    edits: EditSet
    settings: config.Translation
    applied: tuple[rules.Applied, ...]
    refused: tuple[tuple[str, str, str], ...]
    unreadable: tuple[str, ...]
    disabled: tuple[str, ...]

    @property
    def marked(self) -> int:
        return sum(len(work.marks) for work in self.plan.work.values())


def run(
    audit: Audit,
    ground_truth: GroundTruth,
    *,
    root: Path,
    disabled: Collection[str] = (),
) -> Conversion:
    """Convert the suite `audit` describes, reading and rewriting sources under `root`."""
    built = plan.build(audit, ground_truth, root=root)
    active = rules.enabled(disabled)

    file_edits: list[Edit] = []
    applied: list[rules.Applied] = []
    refused: list[tuple[str, str, str]] = []
    unreadable: list[str] = []

    for path in built.files:
        work = built.work[path]
        source = _read(root, path)
        if source is None and not work.declares:
            unreadable.append(path)
            continue
        try:
            module = lift.apply(cst.parse_module(source or ""), work.fixtures)
            module = _with_copies(module, work)
        except cst.ParserSyntaxError:
            unreadable.append(path)
            continue
        if not source:
            # A module parsed from nothing ends without a newline, whether the file was missing or
            # empty, and a file this writes a declaration into ends like any other.
            module = module.with_changes(has_trailing_newline=True)

        module, produced, backed_out = _rewrite(module, work, active)
        applied.extend(produced)
        refused.extend((path, symbol, code) for symbol, code in backed_out)
        file_edits.append(_edit(root, work, module.code, source))

    claimed = {edit.path for edit in file_edits}
    file_edits.extend(
        Edit(path=path, new_text="", old_text=None)
        for path in built.packages
        if path not in claimed and _read(root, path) is None
    )
    settings = config.translate(ground_truth, root=root)
    written = config.edit(settings, root=root)
    if written is not None:
        file_edits.append(written)

    return Conversion(
        plan=built,
        edits=EditSet(tuple(file_edits)),
        settings=settings,
        applied=tuple(applied),
        refused=tuple(refused),
        unreadable=tuple(unreadable),
        disabled=tuple(sorted(disabled)),
    )


def _rewrite(
    module: cst.Module, work: FileWork, active: Sequence[rules.Rule]
) -> tuple[cst.Module, list[rules.Applied], tuple[tuple[str, str], ...]]:
    module, applied = rules.apply_all(module, active, work.context)

    swapped = wiring.apply(
        module,
        work,
        needs={*work.needs, *(needed for record in applied for needed in record.needs)},
        touched=bool(applied) or bool(work.declares) or bool(work.solo),
    )
    module = swapped.module

    if work.solo:
        module = solo.apply(module, work.solo)
    if work.declares:
        # After the swap, which is what puts the imports the declaration names in the module, and
        # what turns the file's own autouse fixtures into the objects it declares.
        module = declarations.apply(module, work.declares)
    if work.fixtures or work.tests:
        module = wiring.tidy(module)
    module = markers.mark(module, _wanted(work, applied, swapped.refused))
    return module, applied, swapped.refused


def _with_copies(module: cst.Module, work: FileWork) -> cst.Module:
    """`module` with each specialized copy written into it, in the pytest spelling it came in.

    A copy goes below the last thing it names, because that is usually the override it was
    specialized for and a `Depends()` naming it is read where the copy's own `def` is — and, for
    an override a test class wrote, above that class, whose methods read their defaults while its
    body runs. A copy the module already binds was written by an earlier conversion, which is what
    leaves a converted tree alone.
    """
    bound = set(layout.module_level_names(module.code))
    body = list(module.body)
    for copy in work.duplicates:
        if copy.symbol in bound:
            continue
        bound.add(copy.symbol)
        body.insert(min(_below(body, copy.after), _above(body, copy.before)), _definition(copy))
    return module.with_changes(body=body)


def _below(body: Sequence[cst.BaseStatement], names: Collection[str]) -> int:
    """Where in `body` a definition naming `names` can be read, which is under the last of them."""
    written = [
        index
        for index, statement in enumerate(body)
        if isinstance(statement, cst.FunctionDef | cst.ClassDef) and statement.name.value in names
    ]
    return max(written) + 1 if written else len(body)


def _above(body: Sequence[cst.BaseStatement], group: str | None) -> int:
    """Where in `body` the class `group` names is, which nothing it reads can be written below."""
    if group is None:
        return len(body)
    return next(
        (
            index
            for index, statement in enumerate(body)
            if isinstance(statement, cst.ClassDef) and statement.name.value == group
        ),
        len(body),
    )


def _definition(copy: plan.Duplicate) -> cst.FunctionDef:
    """The copy's source as one statement, spaced from what it follows the way a formatter is."""
    statement = cst.parse_module(copy.code).body[0]
    assert isinstance(statement, cst.FunctionDef)
    return statement.with_changes(leading_lines=[cst.EmptyLine(), cst.EmptyLine()])


def _wanted(
    work: FileWork, applied: Collection[rules.Applied], refused: Collection[tuple[str, str]]
) -> Mapping[str, set[str]]:
    """Every marker this file earns, by the qualified name it attaches above."""
    wanted: dict[str, set[str]] = {}
    for qualname, code in work.marks:
        wanted.setdefault(qualname, set()).add(code)
    for symbol, code in refused:
        wanted.setdefault(symbol, set()).add(code)
    for record in applied:
        construct = matrix.construct(record.code)
        if construct.disposition is matrix.Disposition.MECHANICAL:
            continue
        wanted.setdefault(record.qualname or "", set()).add(record.code)
    return wanted


def _edit(root: Path, work: FileWork, new_text: str, source: str | None) -> Edit:
    if not work.moved:
        return Edit(path=work.path, new_text=new_text, old_text=source)
    return Edit(
        path=work.target,
        new_text=new_text,
        old_text=_read(root, work.target),
        moved_from=work.path,
    )


def _read(root: Path, path: str) -> str | None:
    try:
        return Path(root, path).read_text(encoding="utf-8")
    except OSError:
        return None
