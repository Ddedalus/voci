"""The contract between the extractor and everything downstream of it.

The extractor runs in the suite's own environment and the rest of the tool runs anywhere, so a
dump is the only thing they share and nothing checks that the two halves agree except this
module. `load` refuses a dump it cannot vouch for rather than letting a stale or foreign file
half-work.
"""

from __future__ import annotations

import json
from pathlib import Path

# The dump shape this reader understands, matching `extractor.EXTRACTOR_VERSION`. A dump stamped
# with anything else is refused: the two are versioned together so that a field can be given a
# new meaning without any chance of an old dump being read under the new one.
EXTRACTOR_VERSION = 1

# The pytest range the extractor supports, restated here so a dump carried in from elsewhere is
# checked against it too.
MIN_PYTEST = (8, 4)
MAX_PYTEST_EXCLUSIVE = (10,)

# Key to the JSON type it must hold. Absence and wrong type are both refusals, because every one
# of these is read unconditionally when the model is built.
_TOP_LEVEL: dict[str, type | tuple[type, ...]] = {
    "extractor_version": int,
    "pytest_version": str,
    "environment": dict,
    "rootpath": str,
    "inipath": (str, type(None)),
    "args": list,
    "ini": dict,
    "ini_aliases": dict,
    "plugins": dict,
    "exit_status": int,
    "collection_errors": list,
    "autouse_by_node": dict,
    "fixture_defs": dict,
    "fixture_registry": dict,
    "items": list,
}

_FIXTURE_DEF_KEYS = frozenset(
    {
        "argname",
        "scope",
        "params",
        "ids",
        "autouse",
        "visibility",
        "kind",
        "direct_param",
        "argnames",
        "func",
    }
)

_ITEM_KEYS = frozenset(
    {
        "nodeid",
        "path",
        "originalname",
        "own_markers",
        "markers_with_origin",
        "usefixtures",
        "autouse",
    }
)


# The exit statuses that mean collection reached the end: all passed, some test failed, and
# nothing was collected. A failing test says nothing about whether the suite was read correctly,
# which is all a dump claims. Interrupted, internal error and usage error are refused.
CLEAN_EXIT_STATUSES = frozenset({0, 1, 5})


class DumpError(Exception):
    """A dump that cannot be trusted to mean what this version of the tool would read into it."""


def load(path: str | Path) -> dict:
    """The validated contents of the dump at `path`.

    Raises `DumpError` for a missing, unparseable, wrongly-versioned or structurally incomplete
    dump. A returned mapping is guaranteed to have every top-level key at the right type; it is
    not guaranteed to be internally consistent, which is the model's business.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DumpError(
            f"No ground-truth dump at {path}. Run `velox-migrate extract` against the suite, "
            "or run pytest with `-p velox_migrate.extractor --collect-only` in the environment "
            "where the suite collects."
        ) from None
    except OSError as exc:
        raise DumpError(f"Could not read the ground-truth dump at {path}: {exc}") from exc
    return loads(text, source=str(path))


def loads(text: str, *, source: str = "<string>") -> dict:
    """The validated dump encoded in `text`. As `load`, without touching the filesystem."""
    try:
        dump = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DumpError(
            f"{source} is not valid JSON ({exc}). A dump truncated in transit reads like this; "
            "re-run the extractor and copy the whole file."
        ) from exc

    if not isinstance(dump, dict):
        raise DumpError(f"{source} holds a {type(dump).__name__}, not a ground-truth dump.")

    _check_version(dump, source)
    _check_top_level(dump, source)
    _check_pytest_version(dump, source)
    _check_collection(dump, source)
    _check_fixture_defs(dump, source)
    _check_items(dump, source)
    return dump


def _check_version(dump: dict, source: str) -> None:
    found = dump.get("extractor_version")
    if found == EXTRACTOR_VERSION:
        return
    if found is None:
        raise DumpError(
            f"{source} carries no `extractor_version`, so it did not come from velox-migrate's "
            "extractor. Re-run the extractor to produce a dump this tool can read."
        )
    direction = "older" if isinstance(found, int) and found < EXTRACTOR_VERSION else "newer"
    raise DumpError(
        f"{source} was written by extractor version {found}, and this velox-migrate reads "
        f"version {EXTRACTOR_VERSION}. The dump is {direction} than the tool: re-run the "
        "extractor shipped with this velox-migrate against the suite."
    )


def _check_top_level(dump: dict, source: str) -> None:
    for key, expected in _TOP_LEVEL.items():
        if key not in dump:
            raise DumpError(f"{source} is missing the `{key}` section; the dump is incomplete.")
        if not isinstance(dump[key], expected):
            names = expected if isinstance(expected, tuple) else (expected,)
            wanted = " or ".join(t.__name__ for t in names)
            raise DumpError(
                f"{source} has `{key}` as {type(dump[key]).__name__}, expected {wanted}."
            )


def _check_pytest_version(dump: dict, source: str) -> None:
    raw = dump["pytest_version"]
    version = parse_version(raw)
    if version is None:
        raise DumpError(f"{source} records an unreadable pytest version {raw!r}.")
    if not (MIN_PYTEST <= version < MAX_PYTEST_EXCLUSIVE):
        supported = f">={_render(MIN_PYTEST)},<{_render(MAX_PYTEST_EXCLUSIVE)}"
        raise DumpError(
            f"{source} was extracted under pytest {raw}, outside the supported range "
            f"{supported}. Extract again under a supported pytest."
        )


def _check_collection(dump: dict, source: str) -> None:
    """Refuse a dump taken from a collection that did not finish.

    A module pytest could not import contributes no tests, and nothing later in the pipeline can
    tell the difference between a suite that is missing them and a suite that never had them.
    """
    # Named paths before a bare exit code: a failed collection usually sets both, and knowing
    # which modules failed is what someone can act on.
    errors = dump["collection_errors"]
    if not errors:
        status = dump["exit_status"]
        if status not in CLEAN_EXIT_STATUSES:
            raise DumpError(
                f"{source} was written from a pytest run that exited {status} before finishing "
                "collection. Whatever pytest reported has to be fixed before the suite has a "
                "ground truth to migrate from."
            )
        return
    listing = "\n  ".join(str(nodeid) for nodeid in errors[:10])
    remainder = f"\n  ...and {len(errors) - 10} more" if len(errors) > 10 else ""
    raise DumpError(
        f"{source} was written from a collection that failed for {len(errors)} "
        f"path(s):\n  {listing}{remainder}\n"
        "The dump therefore describes only part of the suite. Fix collection under pytest and "
        "extract again — a partial dump migrates a partial suite without saying so."
    )


def _check_fixture_defs(dump: dict, source: str) -> None:
    for key, entry in dump["fixture_defs"].items():
        if not isinstance(entry, dict):
            raise DumpError(f"{source}: fixture definition {key!r} is not an object.")
        missing = _FIXTURE_DEF_KEYS - entry.keys()
        if missing:
            raise DumpError(f"{source}: fixture definition {key!r} is missing {_listing(missing)}.")
        if not isinstance(entry.get("func"), dict):
            raise DumpError(f"{source}: fixture definition {key!r} has no function location.")


def _check_items(dump: dict, source: str) -> None:
    for position, item in enumerate(dump["items"]):
        if not isinstance(item, dict):
            raise DumpError(f"{source}: item at position {position} is not an object.")
        missing = _ITEM_KEYS - item.keys()
        if missing:
            nodeid = item.get("nodeid", f"<position {position}>")
            raise DumpError(f"{source}: item {nodeid} is missing {_listing(missing)}.")


def parse_version(raw: str) -> tuple[int, ...] | None:
    """`raw`'s leading numeric release fields, or `None` if it has none.

    Trailing suffixes are dropped, so a development version such as `"9.2.0.dev123"` compares as
    the release it is heading for.
    """
    fields: list[int] = []
    for field in str(raw).split("."):
        digits = ""
        for char in field:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        fields.append(int(digits))
    return tuple(fields) if fields else None


def _render(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def _listing(names) -> str:
    return ", ".join(f"`{name}`" for name in sorted(names))
