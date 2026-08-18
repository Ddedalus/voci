"""The conversion plan the CLI prints before the diff.

A diff shows what changed line by line; this shows the decisions behind it — where each fixture
module lands, what each translated construct was, and which tests keep their pytest source and why.
Read together, they are what a reviewer needs before allowing the write.
"""

from __future__ import annotations

from collections.abc import Iterable

from velox_migrate import matrix
from velox_migrate.convert import DEFERRED, Conversion

_NAMED = 12


def plan(conversion: Conversion) -> str:
    """The plan as plain text, with no trailing newline."""
    groups = [
        _counts(conversion),
        _layout(conversion),
        _declared(conversion),
        _translated(conversion),
        _refused(conversion),
        _settings(conversion),
        _gaps(conversion),
    ]
    return "\n\n".join("\n".join(group) for group in groups if group)


def _counts(conversion: Conversion) -> list[str]:
    built = conversion.plan
    tests = len(built.blocked_tests)
    fixtures = len(built.layout.homes)
    changed = len(conversion.edits.changes)
    return [
        f"{fixtures} fixtures translated, {len(built.blocked_fixtures)} left as pytest wrote them",
        f"{tests} tests keep their pytest source; the rest convert",
        f"{changed} files change, {conversion.marked} markers written",
    ]


def _layout(conversion: Conversion) -> list[str]:
    moves = conversion.plan.layout.moves
    if not moves:
        return []
    lines = ["fixture modules:"]
    lines += [f"  {source} -> {target}" for source, target in sorted(moves.items())][:_NAMED]
    if len(moves) > _NAMED:
        lines.append(f"  …and {len(moves) - _NAMED} more")
    aliased = [
        (consumer, item)
        for (consumer, _), item in sorted(conversion.plan.layout.imports.items())
        if item.alias
    ]
    lines += [f"  {consumer}: {item}" for consumer, item in aliased[:_NAMED]]
    return lines


def _declared(conversion: Conversion) -> list[str]:
    """Where each `velox.use(...)` goes, since a declaration reaches tests that never name it."""
    built = conversion.plan
    if not built.declarations:
        return []
    lines = ["declarations:"]
    for declaration in built.declarations[:_NAMED]:
        work = built.work.get(declaration.container)
        names = ", ".join(work.declares if work is not None else ())
        lines.append(f"  {declaration.container}: velox.use({names})")
    if len(built.declarations) > _NAMED:
        lines.append(f"  …and {len(built.declarations) - _NAMED} more")
    created = [
        edit.path
        for edit in conversion.edits.changes
        if edit.kind == "create" and edit.path in built.packages
    ]
    if created:
        lines.append(f"  packages created: {', '.join(created[:_NAMED])}")
    return lines


def _translated(conversion: Conversion) -> list[str]:
    counted = _tally(record.code for record in conversion.applied)
    if not counted:
        return []
    return ["translated:", *_rows(counted)]


def _refused(conversion: Conversion) -> list[str]:
    codes = [
        finding.code for finding in conversion.plan.refusals if not finding.construct.suite_level
    ]
    # A construct the matrix refuses outright and one a later phase of the tool translates read
    # the same in the source, and differently to someone deciding whether to wait for the tool.
    by_design = _tally(code for code in codes if code not in DEFERRED)
    deferred = _tally(code for code in codes if code in DEFERRED)
    lines: list[str] = []
    if by_design:
        lines += ["refused:", *_rows(by_design)]
    if deferred:
        lines += ["needs a hand edit:", *_rows(deferred)]
    if conversion.refused:
        lines += [
            f"  {path} ({symbol}): {code}" for path, symbol, code in conversion.refused[:_NAMED]
        ]
    return lines


def _settings(conversion: Conversion) -> list[str]:
    settings = conversion.settings
    lines = ["[tool.velox]:"]
    if settings.conflict:
        return [f"[tool.velox]: left alone — {settings.conflict}"]
    lines += [f"  {key} = {value}" for key, value in settings.settings.items()]
    # Named for what this section can answer for, which is the table: a setting outside it is not
    # necessarily a setting the migration loses — `usefixtures` becomes a declaration and
    # `xfail_strict` is written into each `@velox.xfail`, both of which the sections above show.
    if settings.dropped:
        lines.append(f"  not carried here: {', '.join(settings.dropped)}")
    return lines


def _gaps(conversion: Conversion) -> list[str]:
    lines = []
    if conversion.unreadable:
        lines.append(
            f"unread sources ({len(conversion.unreadable)}): "
            f"{', '.join(sorted(conversion.unreadable)[:_NAMED])}"
        )
    if conversion.disabled:
        lines.append(f"rules disabled: {', '.join(conversion.disabled)}")
    return lines


def _tally(codes: Iterable[str]) -> dict[str, int]:
    counted: dict[str, int] = {}
    for code in codes:
        counted[code] = counted.get(code, 0) + 1
    return counted


def _rows(counted: dict[str, int]) -> list[str]:
    ordered = sorted(counted.items(), key=lambda entry: (-entry[1], entry[0]))
    width = max(len(str(count)) for _, count in ordered)
    return [
        f"  {code}  {str(count).rjust(width)}  {matrix.construct(code).subject}"
        for code, count in ordered
    ]
