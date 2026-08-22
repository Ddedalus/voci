"""Plain factories for the `test_convert*.py` modules: corpus paths and the cached conversion of
each corpus suite. No pytest fixtures live here — see conftest.py.

`conversion_of()` is the expensive step most of those modules only want the answer to: a libcst
parse of a corpus suite, audited and rewritten against one of the checked-in pytest dumps. Pinned
to the checked-in corpus tree, which never changes underfoot, it is pure in
`(suite, version, budget)` — so it is cached once here, shared across every test module that asks
for the same `(suite, version)`, rather than recomputed per module or per test.
"""

from __future__ import annotations

import functools
import shutil
from pathlib import Path

from velox_migrate import audit, convert, model

CORPUS = Path(__file__).resolve().parents[1] / "corpus"
DUMPS = CORPUS / "dumps"
PYTEST_VERSIONS = ["8.4", "9.1"]


@functools.cache
def _conversion_of_corpus(suite: str, version: str, budget: int) -> convert.Conversion:
    """conversion_of, pinned to the checked-in corpus tree, which never changes underfoot.

    That makes it pure in (suite, version, budget), so it's cached: dozens of tests spread across
    the convert test modules each want their own `conversion_of(suite, version)` and would
    otherwise redo the same libcst parse and rewrite.
    """
    where = CORPUS / suite
    ground_truth = model.load(DUMPS / f"{suite}-pytest-{version}.json")
    return convert.run(audit.run(ground_truth, root=where, budget=budget), ground_truth, root=where)


def conversion_of(
    suite: str, version: str, *, root: Path | None = None, budget: int = audit.DEFAULT_BUDGET
) -> convert.Conversion:
    if root is None:
        return _conversion_of_corpus(suite, version, budget)
    ground_truth = model.load(DUMPS / f"{suite}-pytest-{version}.json")
    return convert.run(audit.run(ground_truth, root=root, budget=budget), ground_truth, root=root)


def converted(suite: str, version: str, destination: Path) -> convert.Conversion:
    """`suite` copied into `destination` and converted in place, as a user would run it."""
    shutil.copytree(CORPUS / suite, destination, dirs_exist_ok=True)
    # `destination` is an untouched copy of the corpus, so the cached corpus Conversion's edits
    # (rootdir-relative, per EditSet's own contract) apply to it exactly as a fresh one would.
    result = conversion_of(suite, version)
    result.edits.apply(destination)
    return result
