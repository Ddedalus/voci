"""The suite's pytest configuration as a `[tool.velox]` table.

`[tool.velox]` rejects unknown keys, so a setting is either carried under an exact velox spelling
or reported as dropped; nothing is written on the chance that velox might understand it. What
counts as a setting the suite has is what its own ini file says, read through
`audit.config.written_settings` — pytest's resolved values cannot tell a plugin's default from a
line someone wrote, and writing a default back out as configuration would make it the suite's.

A `[tool.velox]` table that is already there is read, compared and left alone, so the one file the
conversion writes outside the tests is also the one file it never overwrites.
"""

from __future__ import annotations

import ast
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from velox_migrate.audit.config import written_settings
from velox_migrate.convert.edits import Edit
from velox_migrate.model import GroundTruth

__all__ = ["CARRIED", "PYPROJECT", "Translation", "edit", "translate"]

#: Where a suite's velox configuration goes, rootdir-relative.
PYPROJECT = "pyproject.toml"

HEADER = "[tool.velox]"

#: pytest setting -> `[tool.velox]` key, for the settings that mean the same thing on both sides:
#: support-matrix rows VX301, VX302, VX303 and VX304.
CARRIED: Mapping[str, str] = {
    "testpaths": "testpaths",
    "python_files": "test_file_patterns",
    "norecursedirs": "ignore",
    "timeout": "timeout",
}

# `velox._config`'s own key order, which the table follows so two conversions read alike.
_ORDER = (
    "testpaths",
    "concurrency",
    "timeout",
    "loop_watchdog",
    "test_file_patterns",
    "ignore",
    "env",
)

# The type each carried velox key takes, as `velox._config` validates it.
_LISTS = frozenset({"testpaths", "test_file_patterns", "ignore"})

# What a TOML basic string cannot hold literally. Any other control character means the value has
# no rendering this writes, and it is dropped rather than mangled.
_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}

# fnmatch's metacharacters. An entry holding one is a pattern, not a directory name.
_GLOB = re.compile(r"[*?\[\]]")

_TABLE = re.compile(r"^\[tool\.velox(\.|\])", re.MULTILINE)
_KEY = re.compile(r"^([A-Za-z0-9_-]+)\s*=")
_SUBTABLE = re.compile(r"^\[tool\.velox\.([^\]]+)\]")


@dataclass(frozen=True, slots=True)
class Translation:
    """What one suite's pytest configuration becomes under velox.

    `settings` is velox key -> TOML value text in the order `table` writes them, and `carried` says
    which pytest setting each of them came from. `dropped` names the settings the suite wrote that
    velox has no key for, together with any whose value could not be rendered — a value is never
    guessed at. `conflict` says why an existing `[tool.velox]` was left as it is, and is `None` both
    when there is no such table and when the one there is what this would have written.
    """

    settings: Mapping[str, str]
    carried: Mapping[str, str]
    dropped: tuple[str, ...]
    conflict: str | None
    table: str


def translate(ground_truth: GroundTruth, *, root: Path) -> Translation:
    """The `[tool.velox]` table for the suite `ground_truth` describes, whose files are at `root`.

    `root` is where the suite's own ini file is read and where an existing `pyproject.toml` is
    looked for. Both are only read; `edit` is what produces the change to the tree.
    """
    settings: dict[str, str] = {}
    carried: dict[str, str] = {}
    written = _written(ground_truth, root)
    for pytest_key in sorted(written & set(CARRIED), key=lambda key: _ORDER.index(CARRIED[key])):
        velox_key = CARRIED[pytest_key]
        value = _value(ground_truth, pytest_key, velox_key)
        if value is None:
            continue
        settings[velox_key] = value
        carried[pytest_key] = velox_key
    table = _render(settings)
    return Translation(
        settings=settings,
        carried=carried,
        dropped=tuple(sorted(written - set(carried))),
        conflict=_conflict(_existing(root), settings, table),
        table=table,
    )


def edit(translation: Translation, *, root: Path) -> Edit | None:
    """The `pyproject.toml` edit that puts `translation`'s table in the tree at `root`.

    `None` when the file already declares `[tool.velox]`, whether or not it declares the same
    thing: `translation.conflict` is what tells those two apart.
    """
    text = _read(Path(root, PYPROJECT))
    if text is None:
        return Edit(path=PYPROJECT, new_text=translation.table, old_text=None)
    if _table_of(text) is not None:
        return None
    return Edit(path=PYPROJECT, new_text=_appended(text, translation.table), old_text=text)


def _written(ground_truth: GroundTruth, root: Path) -> set[str]:
    """The settings the suite's own ini file sets, empty when that file cannot be read.

    Nothing is carried out of pytest's resolved configuration, which reports a value for every
    setting pytest and its plugins register: `norecursedirs` reads the same there whether the suite
    wrote it or not, and turning that into a velox `ignore` key would hand the suite a setting it
    never had.
    """
    settings, source = written_settings(ground_truth, root)
    return settings if source == ground_truth.inipath else set()


def _value(ground_truth: GroundTruth, pytest_key: str, velox_key: str) -> str | None:
    """`pytest_key`'s value as the TOML text `velox_key` takes, or `None` if it has no such text.

    The dump carries what pytest resolved the setting to as a `repr`, so the value comes back
    through `literal_eval`; a `repr` of an object no literal describes belongs to a setting this
    cannot translate.
    """
    try:
        resolved = ground_truth.ini_value(pytest_key)
    except KeyError:
        return None
    try:
        value = ast.literal_eval(resolved)
    except (SyntaxError, TypeError, ValueError):
        return None
    if velox_key not in _LISTS:
        return _number(value)
    return _list(_plain_names(value) if velox_key == "ignore" else value)


def _plain_names(value: object) -> object:
    """`norecursedirs` narrowed to the entries velox's `ignore` can honour.

    pytest matches `norecursedirs` as fnmatch patterns against each directory it walks into;
    velox's `ignore` is a set of directory names it compares exactly. A pattern carried across
    verbatim would stop excluding what it excluded, so `.*`, `*.egg` and `build[0-9]` are dropped
    and reported rather than written as names nothing will ever equal.
    """
    if not isinstance(value, list | tuple):
        return value
    return [item for item in value if not (isinstance(item, str) and _GLOB.search(item))]


def _list(value: object) -> str | None:
    """A list setting as TOML, or `None` when it is not a list of strings this can write.

    An empty list is nothing to carry: pytest resolves a setting written with no value to one, and
    an empty velox list is an instruction — collect nothing, ignore nothing — rather than silence.
    """
    if not isinstance(value, list | tuple) or not value:
        return None
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        text = _string(item)
        if text is None:
            return None
        items.append(text)
    return f"[{', '.join(items)}]"


def _number(value: object) -> str | None:
    """A seconds setting as TOML, or `None` when it is not a finite number.

    pytest-timeout registers its setting as a plain string, so the number usually arrives as one.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        seconds = float(value)
    elif isinstance(value, str):
        try:
            seconds = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(seconds):
        return None
    return str(int(seconds)) if seconds.is_integer() and abs(seconds) < 1e16 else repr(seconds)


def _string(value: str) -> str | None:
    """`value` as a TOML basic string, or `None` when it holds a character none can hold."""
    out: list[str] = []
    for char in value:
        escaped = _ESCAPES.get(char)
        if escaped is not None:
            out.append(escaped)
        elif char < " " or char == "\x7f":
            return None
        else:
            out.append(char)
    return f'"{"".join(out)}"'


def _render(settings: Mapping[str, str]) -> str:
    """The block to write: the header, then one line per setting, ending in one newline.

    The header goes in even when there is nothing under it. `[tool.velox]` is what marks the
    rootdir, so a suite without one is run against whichever `pyproject.toml` sits above it — its
    repository's, or none at all.
    """
    return "".join(f"{line}\n" for line in (HEADER, *(f"{k} = {v}" for k, v in settings.items())))


def _existing(root: Path) -> str | None:
    """The `[tool.velox]` block already in `<root>/pyproject.toml`, if there is one."""
    text = _read(Path(root, PYPROJECT))
    return None if text is None else _table_of(text)


def _table_of(text: str) -> str | None:
    """The `[tool.velox]` block of `text`, sub-tables included, or `None` if it declares none.

    Ends at the next table header that is not velox's own, with the blank lines before it dropped,
    so the block compares equal to a freshly rendered one however it was spaced in the file.
    """
    found = _TABLE.search(text)
    if found is None:
        return None
    lines = text[found.start() :].splitlines()
    block = [lines[0]]
    for line in lines[1:]:
        if line.startswith("[") and not _TABLE.match(line):
            break
        block.append(line)
    while block and not block[-1].strip():
        block.pop()
    return "".join(f"{line}\n" for line in block)


def _conflict(existing: str | None, settings: Mapping[str, str], table: str) -> str | None:
    """Why the `[tool.velox]` table in the file is kept over the one this would write."""
    if existing is None or existing == table:
        return None
    theirs = ", ".join(key for line in existing.splitlines() if (key := _key_of(line)))
    return (
        f"{PYPROJECT} already configures velox, setting {theirs or 'nothing'}, where this "
        f"conversion would set {', '.join(settings) or 'nothing'}. Its table is left as it is."
    )


def _key_of(line: str) -> str | None:
    """What `line` sets, whether it sets a key or opens a sub-table like `[tool.velox.env]`."""
    found = _KEY.match(line) or _SUBTABLE.match(line)
    return None if found is None else found.group(1)


def _appended(text: str, table: str) -> str:
    """`text` with `table` after a blank line, the rest of the file byte for byte as it was."""
    if not text.strip():
        return table
    body = text if text.endswith("\n") else f"{text}\n"
    return body + table if body.endswith("\n\n") else f"{body}\n{table}"


def _read(path: Path) -> str | None:
    """`path`'s text, or `None` when there is no file to read there."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None
