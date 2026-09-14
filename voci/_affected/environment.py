"""M4's real environment key (`plans/affected-tests-plan.md`, Environment key design section): a
mismatch means a full run, so the key only needs to cover what a test's own dependency closure
can't already attribute to itself -- the interpreter, the locale, the resolved `[tool.voci]` plus
the CLI options that change what every test sees, and first-party compiled extensions, which carry
no source `DefKey`/`NameKey` closure could ever reach.

Built in one pass, `env_key`, over pieces small enough to keep separate for testing:

- `_interpreter_fingerprint`: implementation name, full version, ABI flags, `sys.platform` --
  "interpreter implementation, full version, ABI flags, and sys.platform".
- `_config_fingerprint`: every `[tool.voci]` key (`_config.Config`'s own fields, minus the ones
  that only say *where* it was found -- `rootdir`/`source`/`anchored`/`git_root` are the store's
  own concern, not the suite's), hashed together with the CLI options that layer on top of it and
  aren't already covered elsewhere: `-W`, `--timeout`, concurrency and assert mode (`--concurrency`
  itself, `--serial`, and `[tool.voci] concurrency` all resolve to one effective int by the time a
  caller has it -- `cli._resolve_options` -- so this hashes that resolved value, not the three ways
  of spelling it).
- `_locale_fingerprint`: `LANG`, every POSIX `LC_*` category, and `TZ` -- read directly from
  `os.environ`, not through the recorder (`environ.py`): these change what C-level calls do without
  any first-party code reading them itself for `EnvKey` to ever see.
- `_extension_fingerprint`: a content hash of every first-party compiled extension module
  (`*.so`/`*.pyd`) under rootdir -- `world.first_party_files`'s own walk, widened past `*.py`, since
  a `.so`'s machine code has no source `DefKey`/`NameKey` to key a fingerprint change against at
  all otherwise.

**Known gap, deliberately deferred rather than left silent:** "the entry points of every group
queried during the run" (Environment key design section) needs a group's contents *as of this run*
before the run has necessarily queried it even once -- computable only after the fact, unlike
every other piece here, all of which are known before a single test runs. Until a persisted,
across-runs record of which groups a suite has ever queried exists to seed the *next* run's key
with, this key omits entry points outright; a newly installed plugin therefore isn't itself cause
for a full run yet, though nothing about the fields already here becomes unsound for omitting it
-- exactly the placeholder's own "too coarse costs extra full runs, never an unsound skip" logic,
just for one field rather than the whole key.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from collections.abc import Sequence
from pathlib import Path

from voci._affected.tracer import is_first_party
from voci._collection.discovery import discover_files
from voci._config import Config

__all__ = ["env_key"]

#: LANG and TZ aren't POSIX "LC_*" categories, but C reads them the same way -- without going
#: through `os.environ` -- so they belong in the same fingerprint (Environment key design section:
#: "LANG, LC_* and TZ, which C reads without going through os.environ").
_LOCALE_VARS = (
    "LANG",
    "LC_ALL",
    "LC_COLLATE",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LC_MONETARY",
    "LC_NUMERIC",
    "LC_TIME",
    "TZ",
)


def env_key(
    rootdir: Path,
    config: Config,
    *,
    concurrency: int,
    timeout: float | None,
    filterwarnings: Sequence[str],
    assert_mode: str,
) -> str:
    """This run's environment key: a mismatch against a stored one means a full run (Selection
    design section), so every piece here is something that changes what *any* test sees without
    that test's own dependency closure ever naming it. `concurrency`/`timeout`/`filterwarnings`
    are the already-three-tier-resolved values (`cli._PreparedRun`'s own fields) -- CLI >
    `[tool.voci]` > built-in default folded into one, the same value every test in this run
    actually ran under, not `config`'s own possibly-`None` fields, which by themselves don't say
    whether a flag or a built-in default won the tie."""
    parts = [
        _interpreter_fingerprint(),
        _config_fingerprint(config),
        f"concurrency={concurrency}",
        f"timeout={timeout}",
        f"assert_mode={assert_mode}",
        f"filterwarnings={list(filterwarnings)}",
        _locale_fingerprint(),
        _extension_fingerprint(rootdir),
    ]
    return "|".join(parts)


def _interpreter_fingerprint() -> str:
    return "-".join(
        [
            sys.implementation.name,
            platform.python_version(),
            sys.abiflags,
            sys.platform,
        ]
    )


def _config_fingerprint(config: Config) -> str:
    payload = {
        "testpaths": config.testpaths,
        "concurrency": config.concurrency,
        "timeout": config.timeout,
        "loop_watchdog": config.loop_watchdog,
        "test_file_patterns": config.test_file_patterns,
        "ignore": config.ignore,
        "env": dict(sorted(config.env.items())),
        "filterwarnings": config.filterwarnings,
        "affected_trace_threads": config.affected_trace_threads,
    }
    blob = json.dumps(payload, sort_keys=True, default=list)
    return hashlib.blake2b(blob.encode(), digest_size=8).hexdigest()


def _locale_fingerprint() -> str:
    return "&".join(f"{name}={os.environ.get(name, '')}" for name in _LOCALE_VARS)


def _extension_fingerprint(rootdir: Path) -> str:
    resolved = rootdir.resolve()
    candidates = discover_files([resolved], patterns=("*.so", "*.pyd"))
    hashes = []
    for path in candidates:
        if not is_first_party(str(path), resolved):
            continue
        try:
            digest = hashlib.blake2b(path.read_bytes(), digest_size=8).hexdigest()
        except OSError:
            continue
        hashes.append(f"{path.relative_to(resolved)}:{digest}")
    return "&".join(sorted(hashes))
