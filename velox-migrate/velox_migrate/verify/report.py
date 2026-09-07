"""The three renderings of a verification, mirroring `report/`'s three of an audit.

`terminal` is the summary the command prints, `markdown` is `verify-report.md` — the review queue
a human works through — and `payload`/`write_payload` produce `verify.json` for whatever reads it
next. All three are pure functions of one `Verification`, and every list is ordered, so one pair
of runs always renders to one set of bytes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from velox_migrate.report.markdown import table
from velox_migrate.report.payload import write_json
from velox_migrate.verify import Divergence, Run, Verification

__all__ = ["VERIFY_VERSION", "markdown", "payload", "terminal", "write_markdown", "write_payload"]

VERIFY_VERSION = 1

#: How many divergences of one kind the terminal summary lists before it defers to the report.
TERMINAL_ROWS = 10

_ID_WIDTH = 60

_HEADLINES = {
    "missing": "ran under pytest, never ran under velox",
    "unexpected": "ran under velox, never ran under pytest",
    "outcome": "ran under both and ended differently",
}


def terminal(verification: Verification) -> str:
    """The summary as plain text, with no trailing newline."""
    groups = [_counts(verification), _errors(verification), _queue(verification)]
    return "\n\n".join("\n".join(group) for group in groups if group)


def markdown(verification: Verification) -> str:
    """`verify-report.md`: the whole review queue, nothing elided."""
    before, after = verification.before, verification.after
    lines = [
        "# Verification",
        "",
        f"`{before.runner}` on `{before.tree}` against `{after.runner}` on `{after.tree}`.",
        "",
        table(("", "tests", "outcomes", "wall"), (_run_row(before), _run_row(after))),
        "",
        (
            f"{verification.agreed} agreed, {len(verification.divergences)} diverged."
            if verification.divergences
            else f"{verification.agreed} tests agreed; nothing diverged."
        ),
        "",
    ]
    for run in (before, after):
        if run.collection_errors:
            lines += [
                f"## {run.runner} collection errors",
                "",
                *(f"- `{path}`" for path in run.collection_errors),
                "",
            ]
    for kind, items in verification.by_kind().items():
        lines += [
            f"## {kind} ({len(items)})",
            "",
            f"{_HEADLINES[kind]}.",
            "",
            table(("test", "pytest", "velox"), [_divergence_row(item) for item in items]),
            "",
        ]
    return "\n".join(lines).rstrip("\n") + "\n"


def payload(verification: Verification) -> dict[str, Any]:
    """The verification as JSON-ready data, under the schema `verify_version` names."""
    return {
        "verify_version": VERIFY_VERSION,
        "ok": verification.ok,
        "agreed": verification.agreed,
        "before": _run_payload(verification.before),
        "after": _run_payload(verification.after),
        "divergences": [
            {
                "id": item.id,
                "kind": item.kind,
                "before": item.before,
                "after": item.after,
            }
            for item in verification.divergences
        ],
    }


def write_markdown(verification: Verification, path: Path) -> None:
    path.write_text(markdown(verification), encoding="utf-8")


def write_payload(verification: Verification, path: Path) -> None:
    write_json(payload(verification), path)


def _run_payload(run: Run) -> dict[str, Any]:
    return {
        "runner": run.runner,
        "tree": str(run.tree),
        "tests": len(run.outcomes),
        "outcomes": run.counts(),
        "collection_errors": list(run.collection_errors),
        "exit_status": run.exit_status,
        "wall": round(run.duration, 2),
        "command": list(run.command),
    }


def _counts(verification: Verification) -> list[str]:
    before, after = verification.before, verification.after
    lines = [
        f"{len(before.outcomes)} tests under {before.runner}   "
        f"{len(after.outcomes)} under {after.runner}   "
        f"{verification.agreed} agreed   {len(verification.divergences)} diverged"
    ]
    by_kind = verification.by_kind()
    if by_kind:
        lines.append("   ".join(f"{kind}: {len(items)}" for kind, items in by_kind.items()))
    return lines


def _errors(verification: Verification) -> list[str]:
    lines = []
    for run in (verification.before, verification.after):
        for path in run.collection_errors:
            lines.append(f"{run.runner} could not collect {path}")
    return lines


def _queue(verification: Verification) -> list[str]:
    lines: list[str] = []
    for kind, items in verification.by_kind().items():
        lines.append(f"{kind} ({len(items)}) — {_HEADLINES[kind]}")
        for item in items[:TERMINAL_ROWS]:
            before = item.before or "(not run)"
            after = item.after or "(not run)"
            lines.append(f"  {_clip(item.id).ljust(_ID_WIDTH)}  {before} -> {after}")
        if len(items) > TERMINAL_ROWS:
            lines.append(f"  ... {len(items) - TERMINAL_ROWS} more")
    return lines


def _run_row(run: Run) -> tuple[str, str, str, str]:
    outcomes = ", ".join(f"{count} {outcome}" for outcome, count in run.counts().items())
    return (run.runner, str(len(run.outcomes)), outcomes or "—", f"{run.duration:.2f}s")


def _divergence_row(item: Divergence) -> tuple[str, str, str]:
    return (f"`{item.id}`", item.before or "—", item.after or "—")


def _clip(text: str) -> str:
    """`text` shortened from the left when it is too wide for the column: an id's tail — the test
    name and its `[case]` — is what tells two of them apart, and its head is the path they share.
    """
    return text if len(text) <= _ID_WIDTH else "…" + text[-(_ID_WIDTH - 1) :]
