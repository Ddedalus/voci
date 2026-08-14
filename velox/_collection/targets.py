"""`PATHS` entries: a file, a directory, or a `path.py::test_name` test id.

`parse_target` splits one command-line argument into the file or directory velox discovers from
and, after `::`, the optional selector naming what inside it to run. `IdSelection` is the set of
those selectors for a whole run: it answers, per collected test, whether the run asked for it.

A selector is matched against a test id's own `::`-separated tail, so `TestGroup` selects every
test in that class, `test_name` selects every `@velox.parametrize` case of that function, and
`test_name[case]` selects one case. Everything under a path that carries no selector at all is
selected, which is what makes `velox tests/ tests/test_api.py::test_create` mean "all of tests/,
and specifically that one test's file too".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = ["IdSelection", "Target", "parse_target"]


@dataclass(frozen=True, slots=True)
class Target:
    """One `PATHS` argument, split at its first `::`."""

    raw: str
    path: Path
    """The file or directory part, exactly as written -- `cli.main` is what checks it exists and
    hands it to discovery."""
    selector: str | None
    """What follows `::`, or `None` for a plain path. Kept whole, inner `::` included, so a
    class-grouped id (`TestGroup::test_case`) stays one selector."""


def parse_target(raw: str) -> Target:
    """Split `raw` at its first `::`. A trailing `::` with nothing after it yields a selector of
    `""`, which `cli.main` reports as a usage error rather than silently selecting nothing."""
    path_part, separator, selector = raw.partition("::")
    return Target(raw=raw, path=Path(path_part), selector=selector if separator else None)


@dataclass(frozen=True, slots=True)
class IdSelection:
    """The `path.py::test_name` selectors of one run, keyed by the file they name.

    A file absent from `_by_path` is unconstrained: every test in it is selected. That covers a
    file no argument narrows at all, and a file also reached by a bare argument -- naming a file
    (or a directory holding it) with no selector is the wider request, so it wins and `of` drops
    the narrower entry.
    """

    _by_path: Mapping[Path, tuple[str, ...]]

    @classmethod
    def of(cls, targets: Iterable[Target]) -> IdSelection | None:
        """The selection `targets` describe, or `None` when none of them carries a selector --
        the common case, which callers use to skip id filtering entirely.

        Paths are resolved here, once, so matching later compares absolute paths rather than
        whatever mix of relative and absolute forms the command line held.
        """
        selectors: dict[Path, list[str]] = {}
        unconstrained: list[Path] = []
        for target in targets:
            resolved = target.path.resolve()
            if target.selector is None:
                unconstrained.append(resolved)
            else:
                selectors.setdefault(resolved, []).append(target.selector)
        constrained = {
            path: tuple(names)
            for path, names in selectors.items()
            # `parents`, not equality alone: `velox tests/ tests/test_api.py::test_create` asks
            # for all of tests/, which includes every test of the file the selector names.
            if not any(path == bare or bare in path.parents for bare in unconstrained)
        }
        return cls(_by_path=constrained) if constrained else None

    def selects(self, path: Path, suffix: str) -> bool:
        """Whether a test in `path` (resolved) with id tail `suffix` was asked for.

        Asked of whole ids, `[case]` suffix included, so a selector naming one case of a
        parametrized test can only ever be answered after expansion.
        """
        names = self._by_path.get(path)
        return names is None or any(_matches(suffix, name) for name in names)

    def unmatched(self, ids: Sequence[str], *, rootdir: Path) -> list[str]:
        """The selectors that matched none of `ids` (whole test ids, `path::suffix`), in the
        order the command line gave them.

        A selector naming a test that doesn't exist is a typo, and must not look like an honest
        empty selection any more than a typo'd path does -- `cli.main` reports these as a usage
        error. `rootdir` is what a test id's path part is relative to (`TestRecord.path`), so it
        is what resolves them back to the absolute paths this selection is keyed by.
        """
        suffixes_by_path: dict[Path, list[str]] = {}
        for test_id in ids:
            path_part, _, suffix = test_id.partition("::")
            suffixes_by_path.setdefault((rootdir / path_part).resolve(), []).append(suffix)
        missing: list[str] = []
        for path, names in self._by_path.items():
            suffixes = suffixes_by_path.get(path, [])
            missing.extend(
                name for name in names if not any(_matches(suffix, name) for suffix in suffixes)
            )
        return missing


def _matches(suffix: str, selector: str) -> bool:
    """Whether a test id's `::`-separated tail is the one `selector` asked for: the selector
    itself, a test inside the class it names, or one `@velox.parametrize` case of the function
    it names."""
    return (
        suffix == selector
        or suffix.startswith(f"{selector}::")
        or suffix.startswith(f"{selector}[")
    )
