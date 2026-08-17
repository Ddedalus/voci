"""The file changes a conversion would make, held as data before any of them is written.

One description answers both of the things a caller wants: a diff to read before agreeing to
anything, and a tree to write once they do. Paths are rootdir-relative POSIX throughout and the
tree's root reaches only `apply`, so an edit set describes a suite without being tied to where a
copy of it happens to sit.

An edit whose new text equals the old one is kept but reports no change, and neither the diff nor
`apply` mentions it. That is what makes converting an already-converted tree a visible no-op
rather than a rewrite that happens to land on the same bytes.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

__all__ = ["Edit", "EditSet"]

# git's mode for an ordinary non-executable file, so the rendered diff reads as one git wrote.
_MODE = "100644"
_CONTEXT = 3
_DEV_NULL = "/dev/null"
_NO_NEWLINE = "\\ No newline at end of file"


@dataclass(frozen=True, slots=True)
class Edit:
    """What one file holds after a conversion, against what it holds now.

    `new_text` is `None` to remove the file and `old_text` is `None` when there is no file yet.
    `moved_from` is where the content was written before, which is how a `conftest.py` becomes a
    `fixtures.py`: the new text belongs to `path` and the file at `moved_from` goes away.

    Both paths are rootdir-relative and POSIX-separated; an absolute path, a `..` segment or a
    backslash raises, because everything downstream treats these as keys as much as locations.
    """

    path: str
    new_text: str | None
    old_text: str | None
    moved_from: str | None = None

    def __post_init__(self) -> None:
        _check(self.path)
        if self.moved_from is None:
            return
        _check(self.moved_from)
        if self.moved_from == self.path:
            raise ValueError(f"{self.path}: a move to its own path is not a move.")
        if self.new_text is None:
            raise ValueError(
                f"{self.path}: a removal cannot also be a move from {self.moved_from}."
            )

    @property
    def kind(self) -> str:
        """`"create"`, `"modify"`, `"delete"` or `"move"`."""
        if self.new_text is None:
            return "delete"
        if self.moved_from is not None:
            return "move"
        return "create" if self.old_text is None else "modify"

    @property
    def changed(self) -> bool:
        """Whether applying this would do anything at all.

        A move counts even when the text is identical, since the file itself relocates; a removal
        of a file that is not there counts as nothing.
        """
        return self.moved_from is not None or self.new_text != self.old_text


@dataclass(frozen=True, slots=True)
class EditSet:
    """Every file change one conversion would make, ordered by path.

    The order is imposed here rather than asked of the caller, so two runs that decide the same
    changes render the same bytes. Two edits claiming one path, two moves out of one path, or a
    path that one edit writes and another moves away are all programming errors and raise: each
    would make the result depend on the order `apply` happened to use.
    """

    edits: tuple[Edit, ...]

    def __post_init__(self) -> None:
        written: dict[str, Edit] = {}
        for edit in self.edits:
            if edit.path in written:
                raise ValueError(f"Two edits claim {edit.path}.")
            written[edit.path] = edit
        moved: set[str] = set()
        for edit in self.edits:
            source = edit.moved_from
            if source is None:
                continue
            if source in moved:
                raise ValueError(f"Two edits move {source} away.")
            if source in written:
                raise ValueError(f"{source} is both written and moved away.")
            moved.add(source)
        object.__setattr__(self, "edits", tuple(sorted(self.edits, key=lambda edit: edit.path)))

    @property
    def changes(self) -> tuple[Edit, ...]:
        """The edits with something to do, in path order."""
        return tuple(edit for edit in self.edits if edit.changed)

    def diff(self) -> str:
        """Every change as one git-style unified diff, or `""` when nothing changed.

        Three lines of context, one section per file, `/dev/null` for the side a create or a delete
        has no file on, and `rename from`/`rename to` for a move. Ends in a newline when non-empty.
        """
        return "".join(_section(edit) for edit in self.changes)

    def apply(self, root: Path) -> tuple[str, ...]:
        """Make the changes in the tree at `root`, and report the paths written and removed, sorted.

        Every file is written, parent directories included, before anything is removed, so a move
        cannot lose content by deleting its source before its target exists. Each file is replaced
        in one step: an interrupted run leaves files it had not reached yet, never half of one.
        """
        changes = self.changes
        touched = {edit.path for edit in changes if edit.new_text is not None}
        for edit in changes:
            if edit.new_text is not None:
                _write(Path(root, edit.path), edit.new_text)
        for edit in changes:
            gone = (edit.moved_from, edit.path if edit.new_text is None else None)
            for victim in gone:
                if victim is not None and _remove(Path(root, victim)):
                    touched.add(victim)
        return tuple(sorted(touched))


def _check(path: str) -> None:
    if not path or path.startswith("/") or "\\" in path or ".." in PurePosixPath(path).parts:
        raise ValueError(f"{path!r} is not a rootdir-relative POSIX path.")


def _write(target: Path, text: str) -> None:
    """`text` into `target`, in one step, creating the directories above it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_name(f".{target.name}.velox-migrate")
    # `newline=""` so the bytes on disk are the bytes the diff showed, on every platform.
    scratch.write_text(text, encoding="utf-8", newline="")
    try:
        os.replace(scratch, target)
    except OSError:
        scratch.unlink(missing_ok=True)
        raise


def _remove(victim: Path) -> bool:
    """Remove `victim`, reporting whether it was there to remove."""
    try:
        victim.unlink()
    except FileNotFoundError:
        return False
    return True


def _section(edit: Edit) -> str:
    """One file's part of the diff, ending in a newline."""
    source = edit.moved_from or edit.path
    lines = [f"diff --git a/{source} b/{edit.path}"]
    if edit.moved_from is not None:
        lines += [f"rename from {edit.moved_from}", f"rename to {edit.path}"]
    elif edit.old_text is None:
        lines.append(f"new file mode {_MODE}")
    elif edit.new_text is None:
        lines.append(f"deleted file mode {_MODE}")
    lines += _hunks(
        edit.old_text or "",
        edit.new_text or "",
        _DEV_NULL if edit.old_text is None else f"a/{source}",
        _DEV_NULL if edit.new_text is None else f"b/{edit.path}",
    )
    return "".join(f"{line}\n" for line in lines)


def _hunks(old: str, new: str, from_label: str, to_label: str) -> list[str]:
    """The `---`/`+++` headers and the hunks between `old` and `new`, empty when they agree."""
    rendered = list(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=from_label,
            tofile=to_label,
            n=_CONTEXT,
            lineterm="",
        )
    )
    if not rendered:
        return []
    # difflib's first two lines are the file headers; after them come hunk headers, which
    # `lineterm=""` leaves bare, and content lines, which carry the newline the file had -- so a
    # content line without one is a file that ends without a newline, as git spells it out.
    lines = rendered[:2]
    for line in rendered[2:]:
        if line.endswith("\n"):
            lines.append(line.removesuffix("\n"))
            continue
        lines.append(line)
        if not line.startswith("@@"):
            lines.append(_NO_NEWLINE)
    return lines
