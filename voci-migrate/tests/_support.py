"""Plain factories for the `test_convert*.py` modules: corpus paths and the cached conversion of
each corpus suite. No pytest fixtures live here — see conftest.py.

`conversion_of()` is the expensive step most of those modules only want the answer to: a libcst
parse of a corpus suite, audited and rewritten against one of the checked-in pytest dumps. Pinned
to the checked-in corpus tree, which never changes underfoot, it is pure in
`(suite, version, budget)` — so it is cached once here, shared across every test module that asks
for the same `(suite, version)`, rather than recomputed per module or per test.

`git`/`git_repo`/`git_commit` are for `test_workspace.py` and the `scaffold` CLI tests, which need
real repositories to rebase against rather than a checked-in dump.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path

from voci_migrate import audit, convert, model

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


_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "voci-migrate tests",
    "GIT_AUTHOR_EMAIL": "tests@example.com",
    "GIT_COMMITTER_NAME": "voci-migrate tests",
    "GIT_COMMITTER_EMAIL": "tests@example.com",
}


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command against a scratch repo, with a fixed local identity so a commit
    succeeds even where the environment has no global `user.name`/`user.email` configured."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **_GIT_IDENTITY},
        capture_output=True,
        text=True,
        check=True,
    )


def git_repo(path: Path) -> Path:
    """An empty git repository at `path`, on branch `main`, ready to commit into."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    return path


def git_commit(path: Path, message: str) -> str:
    """Stage everything under `path` and commit it, returning the new commit's sha."""
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", message)
    return git(path, "rev-parse", "HEAD").stdout.strip()
