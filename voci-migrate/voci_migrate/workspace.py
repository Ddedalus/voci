"""The coexistence workspace: exporting a suite from its own git history into a plain directory,
and snapshotting that directory at the moment `convert --write` crosses from pytest ownership to
voci's.

See plans/migration-tool-plan.md's "Coexistence workspace" section for the shape this backs. In
short: content lives in `source` (the suite's real repository), never in `dest` (a tool-managed
export of it) -- `dest` is cheap to regenerate wholesale and never worth reconciling by hand. The
one thing `dest` carries that `source` doesn't is a relocation fixup, a commit on `RELOCATION_
BRANCH` for whatever only broke because the suite now lives somewhere else. `scaffold` rebases
that branch onto `source`'s tip and exports the result; `snapshot_baseline` is the copy `convert
--write` takes of `dest` the moment before it overwrites it, so a later `--reset` (once it exists)
has something to restore from instead of a lost tree.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from voci_migrate.verify.runners import _tail

#: Created empty at `source`'s tip the first time `scaffold` sees it, then rebased onto that tip
#: on every later call. A relocation fixup -- a path or anything else that only broke because the
#: suite now lives somewhere else -- is a commit here, never on `source`'s own branches.
RELOCATION_BRANCH = "voci-migrate/relocation"

#: This tool's own state directory inside `dest`, holding `BASELINE_DIR` among other things --
#: everywhere `snapshot_baseline` must not recurse into, and the baseline pytest run right after it
#: must not collect back out of.
TOOL_STATE_DIR = ".voci-migrate"
#: Where `convert --write` snapshots `dest`, relative to `dest` itself.
BASELINE_DIR = Path(TOOL_STATE_DIR) / "baseline"
TREE_DIR = "tree"
OUTCOMES_NAME = "pytest-outcomes.json"
_MARKER_NAME = "scaffold.json"

__all__ = [
    "BASELINE_DIR",
    "OUTCOMES_NAME",
    "RELOCATION_BRANCH",
    "TOOL_STATE_DIR",
    "TREE_DIR",
    "ScaffoldResult",
    "WorkspaceError",
    "scaffold",
    "snapshot_baseline",
]


class WorkspaceError(Exception):
    """`source` is not usable as a scaffold input, or the relocation branch does not rebase
    cleanly onto its tip."""


@dataclass(frozen=True)
class ScaffoldResult:
    """What one `scaffold` call did: where it exported to, and what it rebased.

    `relocation_worktree` is where `branch` stays checked out afterwards -- not `source`'s own
    working directory, which is left exactly as the caller had it. A relocation fixup is committed
    there directly, then picked up by the next `scaffold` call.
    """

    dest: Path
    branch: str
    tip: str
    created_branch: bool
    relocation_worktree: Path


def scaffold(
    source: Path, dest: Path | None = None, *, branch: str = RELOCATION_BRANCH
) -> ScaffoldResult:
    """Rebase `branch` onto `source`'s current tip and export the result into `dest` as a plain
    directory -- never a worktree, never a second live checkout `dest` and `source` could drift
    out of sync by hand.

    `dest` defaults to a directory named after `source` in the current directory. Refuses to
    overwrite a `dest` that already holds content this function didn't write there itself, so a
    typo'd path fails loudly rather than clobbering something unrelated.

    Raises `WorkspaceError` when `source` is not a git repository, when the rebase hits a real
    conflict (left for the user to resolve with git's own tooling, at the path named in the
    error), or when an earlier such conflict was never resolved.
    """
    source = source.resolve()
    dest = (dest or Path.cwd() / source.name).resolve()

    _require_worktree(source)
    tip = _git(source, "rev-parse", "HEAD")

    created_branch = not _branch_exists(source, branch)
    if created_branch:
        _git(source, "branch", branch, tip)

    scratch = _scratch_worktree(source, branch)
    if _rebase_in_progress(scratch):
        raise WorkspaceError(
            f"a rebase of {branch} onto {source} is still unresolved in {scratch}. Finish it "
            "there -- `git rebase --continue` once the conflict is resolved, or `git rebase "
            "--abort` to give up on this attempt -- then run scaffold again."
        )
    _connect_scratch_worktree(source, scratch, branch)

    rebased = subprocess.run(
        ["git", "rebase", tip], cwd=scratch, capture_output=True, text=True, check=False
    )
    if rebased.returncode != 0:
        raise WorkspaceError(
            f"{branch} does not rebase cleanly onto {tip[:12]} ({source}). Resolve the conflict "
            f"in {scratch}, then run scaffold again:\n{_tail(rebased)}"
        )

    _export(scratch, dest, source=source, branch=branch)
    return ScaffoldResult(
        dest=dest,
        branch=branch,
        tip=tip,
        created_branch=created_branch,
        relocation_worktree=scratch,
    )


def snapshot_baseline(root: Path) -> Path:
    """Copy `root`'s current tree, minus this tool's own state, into `root/.voci-migrate/
    baseline/tree` and return the baseline directory.

    Meant to be called the moment before `convert --write` overwrites `root` in place -- the last
    point `root` still holds whatever `scaffold` (or a hand-managed copy) put there. Overwrites
    any earlier snapshot: it exists to capture what is about to be lost, not a history of every
    conversion attempt.
    """
    baseline = root / BASELINE_DIR
    tree = baseline / TREE_DIR
    shutil.rmtree(tree, ignore_errors=True)
    tree.mkdir(parents=True)

    tool_state = (root / TOOL_STATE_DIR).resolve()
    for entry in root.iterdir():
        if entry.resolve() == tool_state:
            continue
        destination = tree / entry.name
        if entry.is_dir():
            shutil.copytree(entry, destination, symlinks=True)
        else:
            shutil.copy2(entry, destination)
    return baseline


# --- scaffold internals -------------------------------------------------------------------


def _require_worktree(source: Path) -> None:
    if not source.is_dir():
        raise WorkspaceError(f"{source} is not a directory.")
    completed = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=source,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or completed.stdout.strip() != "true":
        raise WorkspaceError(
            f"{source} is not inside a git work tree. scaffold rebases a relocation branch onto "
            "the suite's own git history, so it needs one."
        )


def _branch_exists(source: Path, branch: str) -> bool:
    completed = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=source,
        check=False,
    )
    return completed.returncode == 0


def _scratch_worktree(source: Path, branch: str) -> Path:
    """Where `branch` is checked out to rebase it -- a fixed, discoverable sibling of `source`
    rather than a random temp directory, so a rebase conflict has somewhere to be resolved and a
    later `scaffold` call can find it again. Keyed by `branch` too, so two relocation branches
    against the same `source` don't fight over one worktree."""
    slug = branch.replace("/", "-")
    return source.parent / f".voci-migrate.{source.name}.{slug}"


def _connect_scratch_worktree(source: Path, scratch: Path, branch: str) -> None:
    """Make sure `scratch` is a git worktree checked out at `branch`, adding it the first time and
    reconnecting to it -- rather than re-adding -- on every later call."""
    if scratch.is_dir() and (scratch / ".git").exists():
        _git(scratch, "checkout", branch)
        return
    if scratch.exists():
        shutil.rmtree(scratch)
    subprocess.run(["git", "worktree", "prune"], cwd=source, capture_output=True, check=False)
    _git(source, "worktree", "add", str(scratch), branch)


def _rebase_in_progress(scratch: Path) -> bool:
    if not (scratch.is_dir() and (scratch / ".git").exists()):
        return False
    git_dir = Path(_git(scratch, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = scratch / git_dir
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


def _export(scratch: Path, dest: Path, *, source: Path, branch: str) -> None:
    marker = dest / TOOL_STATE_DIR / _MARKER_NAME
    if dest.exists():
        if any(dest.iterdir()) and not marker.is_file():
            raise WorkspaceError(
                f"{dest} already has content scaffold didn't write ({marker} is missing). Point "
                "scaffold at an empty or new directory, or remove it yourself first."
            )
        # Everything but `.voci-migrate/` is disposable suite content, wholesale-replaced on
        # every export -- but `.voci-migrate/` can hold a baseline `convert --write` already
        # snapshotted there, which a re-scaffold (to pick up a new prefactor or fixup) must not
        # destroy.
        for entry in dest.iterdir():
            if entry.name == TOOL_STATE_DIR:
                continue
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    else:
        dest.mkdir(parents=True)

    archived = subprocess.run(
        ["git", "archive", "HEAD"], cwd=scratch, capture_output=True, check=False
    )
    if archived.returncode != 0:
        raise WorkspaceError(
            f"git archive failed in {scratch}:\n{archived.stderr.decode(errors='replace')}"
        )
    with tarfile.open(fileobj=BytesIO(archived.stdout)) as archive:
        archive.extractall(dest, filter="data")

    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {"source": str(source), "branch": branch, "tip": _git(scratch, "rev-parse", "HEAD")}
    marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed in {cwd}:\n{_tail(completed)}")
    return completed.stdout.strip()
