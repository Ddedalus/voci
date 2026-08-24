"""Regenerate the ground-truth dumps checked in under `velox-migrate/corpus/dumps/`.

One dump per corpus suite per supported pytest version. Each is produced by copying
`extractor.py` alone into a throwaway directory and running it against a pytest installed in an
isolated environment, which is both how a user with an unreachable suite environment runs it and
a standing check that the file really does stand alone.

    uv run python velox-migrate/scripts/refresh_corpus_dumps.py [--check]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "velox-migrate" / "corpus"
DUMPS = CORPUS / "dumps"
EXTRACTOR = REPO_ROOT / "velox-migrate" / "velox_migrate" / "extractor.py"

SUITES = [
    "bodies_showcase",
    "classes_showcase",
    "declarations_showcase",
    "fixtures_showcase",
    "hazards_showcase",
    "mechanical_showcase",
    "overrides_showcase",
    "parametrize_showcase",
]

# The ends of the supported range. A dump from each is what proves the version shims in
# `extractor.py` absorb the differences rather than passing them downstream. Pinned to exact
# patches: a dump records line numbers inside pytest's own builtin fixtures, so a floating pin
# would make the checked-in artifacts go stale on an upstream release that changed nothing here.
PYTEST_VERSIONS = {"8.4": "pytest==8.4.2", "9.1": "pytest==9.1.1"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="regenerate into a temporary directory and fail if the checked-in dumps differ",
    )
    args = parser.parse_args(argv)

    DUMPS.mkdir(parents=True, exist_ok=True)
    stale = []
    for suite in SUITES:
        for label, requirement in PYTEST_VERSIONS.items():
            target = DUMPS / f"{suite}-pytest-{label}.json"
            produced = _extract(suite, requirement)
            if args.check:
                checked_in = target.read_text(encoding="utf-8") if target.exists() else None
                if checked_in is None or _stable(checked_in) != _stable(produced):
                    stale.append((target.relative_to(REPO_ROOT), checked_in, produced))
            else:
                target.write_text(produced, encoding="utf-8")
                print(f"wrote {target.relative_to(REPO_ROOT)}")

    if stale:
        print("\n".join(_report(stale)), file=sys.stderr)
        return 1
    if args.check:
        print("corpus dumps are current")
    return 0


# Enough of a failing dump to recognize what moved, short enough that fourteen of them still
# read as a CI log.
DIFF_LIMIT = 12


def _report(stale: list[tuple[Path, str | None, str]]) -> list[str]:
    """The `--check` failure, as the lines it prints.

    A dump that is merely out of date is fixed by rerunning the script, so this exists for the
    case that rerunning does not fix: a check that fails on a machine other than the one the
    dumps were taken on. Both the fields that moved and the two environments are named, since
    from a CI log those are the only evidence there is.
    """
    lines = ["These dumps no longer match what the extractor produces:"]
    for relative, checked_in, produced in stale:
        lines.append(f"  {relative}")
        if checked_in is None:
            lines.append("    no such file: this dump has never been taken")
            continue
        differences = list(_differences(_stable(checked_in), _stable(produced), ""))
        for name, before, after in differences[:DIFF_LIMIT]:
            lines.append(f"    {name or '<dump>'}: {_brief(before)} -> {_brief(after)}")
        if len(differences) > DIFF_LIMIT:
            lines.append(f"    ... and {len(differences) - DIFF_LIMIT} more")

    for _, checked_in, produced in stale:
        if checked_in is None:
            continue
        volatile = list(_differences(_taken_on(checked_in), _taken_on(produced), ""))
        if volatile:
            lines.append("  This machine is not the one the dumps were taken on:")
            for name, before, after in volatile[:DIFF_LIMIT]:
                lines.append(f"    {name}: {_brief(before)} -> {_brief(after)}")
        break
    lines.append("Run `just corpus-dumps` and commit the result.")
    return lines


def _taken_on(text: str) -> dict[str, Any]:
    """What `_stable` drops: the machine a dump was taken on rather than the suite it describes.

    Compared only once a dump has already failed, where a difference here is the likeliest
    explanation and the one a CI log cannot otherwise show.
    """
    dump = json.loads(text)
    return {key: dump.get(key) for key in ("pytest_version", "environment", "plugins")}


def _differences(before: Any, after: Any, name: str) -> Iterator[tuple[str, Any, Any]]:
    """Every leaf at which two dumps disagree, as `(name, before, after)`.

    A value that only one of the two has is reported whole rather than descended into, so a
    fixture one dump never saw costs a line instead of a screenful.
    """
    if type(before) is not type(after):
        yield name, before, after
    elif isinstance(before, dict):
        for key in dict.fromkeys([*before, *after]):
            nested = f"{name}.{key}".lstrip(".")
            if key in before and key in after:
                yield from _differences(before[key], after[key], nested)
            else:
                yield nested, before.get(key, "<missing>"), after.get(key, "<missing>")
    elif isinstance(before, list):
        if len(before) != len(after):
            yield f"{name} (length)", len(before), len(after)
        else:
            for index, (one, other) in enumerate(zip(before, after, strict=True)):
                yield from _differences(one, other, f"{name}[{index}]")
    elif before != after:
        yield name, before, after


def _brief(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value)
    return text if len(text) <= 100 else text[:97] + "..."


def _stable(text: str) -> Any:
    """The part of a dump that describes the suite rather than the machine it was taken on.

    A dump records its kernel, its interpreter's version and the plugins that happened to be
    installed, all of which differ between two correct runs on different machines. `--check`
    exists to catch the extractor and the checked-in artifacts drifting apart, so it compares
    what the extractor decides and ignores what the environment decides.
    """
    dump = json.loads(text)
    for volatile in ("environment", "pytest_version", "plugins"):
        dump.pop(volatile, None)
    # Builtin fixtures live under `${prefix}/lib/python3.13/...`, which moves with the
    # interpreter's minor version.
    return json.loads(re.sub(r"/python3\.\d+/", "/python3.x/", json.dumps(dump)))


def _extract(suite: str, requirement: str) -> str:
    suite_path = CORPUS / suite
    # A `.pyc` records the path its source had when it was compiled, and that is the path pytest
    # reports for every fixture in the module. Cached bytecode surviving a moved suite would put
    # a path that no longer exists into the dump.
    for cached in suite_path.rglob("__pycache__"):
        shutil.rmtree(cached)

    with tempfile.TemporaryDirectory() as workdir:
        plugin_dir = Path(workdir)
        shutil.copy(EXTRACTOR, plugin_dir / "extractor.py")
        out = plugin_dir / "dump.json"

        # `PYTEST_ADDOPTS` and friends would reach into the isolated run and change what it
        # collects, which is exactly what a reproducible artifact cannot have.
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("PYTEST_") and key != "EXTRACTOR_OUT"
        }
        env["PYTHONPATH"] = str(plugin_dir)
        # `--isolated --no-project` keeps this run off the workspace environment, so the only
        # thing the extractor can import is the pytest named here.
        subprocess.run(
            [
                "uv",
                "run",
                "--isolated",
                "--no-project",
                "--with",
                requirement,
                "python",
                "-m",
                "pytest",
                str(suite_path),
                "-p",
                "extractor",
                "--collect-only",
                "-q",
                "--extractor-out",
                str(out),
            ],
            cwd=REPO_ROOT,
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        dump = json.loads(out.read_text(encoding="utf-8"))

    # `rootpath` is the one field that is an absolute path by necessity: it is where extraction
    # happened. A checked-in dump would carry this machine's copy of the repo in it, so it is
    # replaced by the suite root it denotes.
    dump["rootpath"] = "."
    return json.dumps(dump, indent=1, sort_keys=False) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
