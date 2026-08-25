"""`verify`: run the suite under both runners and compare the two verdicts test for test.

The conversion's own output is a diff, which says what changed but not whether it still works.
This is the stage that answers that: pytest on the pre-migration tree, `velox --serial` on the
converted one, and every id whose verdict moved between them listed as the review queue.

The id map is identity. `convert` emits pytest's ids verbatim — that is a rule the codegen keeps
precisely so this comparison can be a dictionary lookup — so a test that was renamed rather than
converted shows up here as one id missing and another unexpected, which is the honest reading of
what happened to it.

Comparing at `--concurrency 1` first is deliberate: it separates "the conversion changed what the
suite does" from "the suite does not survive running concurrently". Only once the serial run
agrees is raising concurrency a question about the suite rather than about the tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from velox_migrate.verify.runners import (
    DEFAULT_BASELINE,
    Run,
    RunnerError,
    load_record,
    parse_velox,
    record,
    run_pytest,
    run_velox,
)

__all__ = [
    "DEFAULT_BASELINE",
    "Divergence",
    "Run",
    "RunnerError",
    "Verification",
    "compare",
    "load_record",
    "parse_velox",
    "record",
    "run",
    "run_pytest",
    "run_velox",
]

#: What a divergence is, worst first — the order the review queue is listed in. `missing` leads:
#: a test the converted suite never ran at all is a hole in the verification itself, where a
#: changed verdict is at least a verdict.
KINDS = ("missing", "unexpected", "outcome")


@dataclass(frozen=True)
class Divergence:
    """One test the two runners disagree about. `before`/`after` are `None` where that runner
    never reported on the id at all."""

    id: str
    before: str | None
    after: str | None

    @property
    def kind(self) -> str:
        if self.after is None:
            return "missing"
        if self.before is None:
            return "unexpected"
        return "outcome"

    def __str__(self) -> str:
        return f"{self.id}: {self.before or '(not run)'} -> {self.after or '(not run)'}"


@dataclass(frozen=True)
class Verification:
    """Two runs and everything they disagree about."""

    before: Run
    after: Run
    divergences: tuple[Divergence, ...]
    agreed: int

    @property
    def ok(self) -> bool:
        """True when both runners ran the suite, every test ended the same way under each, and
        neither reported a collection error.

        A comparison of two runs that collected nothing is not ok: a mistyped path leaves both
        runners with nothing to disagree about, and the gate this answers must not pass on it.
        """
        if not self.before.outcomes and not self.after.outcomes:
            return False
        return (
            not self.divergences
            and not self.before.collection_errors
            and not self.after.collection_errors
        )

    def by_kind(self) -> dict[str, tuple[Divergence, ...]]:
        """The divergences grouped, in `KINDS` order, with empty kinds left out."""
        grouped = {
            kind: tuple(item for item in self.divergences if item.kind == kind) for kind in KINDS
        }
        return {kind: items for kind, items in grouped.items() if items}


def compare(before: Run, after: Run) -> Verification:
    """The two runs' disagreements, ordered worst kind first and by id within a kind."""
    ids = set(before.outcomes) | set(after.outcomes)
    divergences = [
        Divergence(
            id=test_id, before=before.outcomes.get(test_id), after=after.outcomes.get(test_id)
        )
        for test_id in ids
        if before.outcomes.get(test_id) != after.outcomes.get(test_id)
    ]
    divergences.sort(key=lambda item: (KINDS.index(item.kind), item.id))
    return Verification(
        before=before,
        after=after,
        divergences=tuple(divergences),
        agreed=len(ids) - len(divergences),
    )


def run(
    *,
    before_tree: Path | None,
    after_tree: Path,
    baseline: Path,
    paths: list[str] | None = None,
    concurrency: int = 1,
    pytest_args: list[str] | None = None,
) -> Verification:
    """Both halves and the comparison. `before_tree` is `None` when the pre-migration tree is
    already gone — `convert --write` rewrites in place — in which case the baseline recorded
    before the conversion stands in for it.

    Raises `RunnerError` for anything that leaves the two sides describing different runs: a
    runner that stopped short, or an argument that would narrow only one of them.
    """
    paths = paths or []
    if before_tree is None:
        if paths or pytest_args:
            raise RunnerError(
                "without `--before` there is no pytest run to narrow: the baseline records "
                "whatever the run that wrote it collected, and narrowing only velox would "
                "report the rest of the suite as tests the conversion lost. Re-record the "
                "baseline over the same selection, or pass `--before`."
            )
        loaded = load_record(baseline)
        before = record(loaded, tree=Path(loaded.get("rootpath", ".")))
    else:
        before = run_pytest(before_tree, out=baseline, paths=paths, extra=list(pytest_args or []))
    after = run_velox(after_tree, paths=paths, concurrency=concurrency)
    return compare(before, after)
