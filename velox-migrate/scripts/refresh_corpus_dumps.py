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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "velox-migrate" / "corpus"
DUMPS = CORPUS / "dumps"
EXTRACTOR = REPO_ROOT / "velox-migrate" / "velox_migrate" / "extractor.py"

SUITES = [
    "declarations_showcase",
    "fixtures_showcase",
    "hazards_showcase",
    "mechanical_showcase",
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
                if not target.exists() or _stable(target.read_text(encoding="utf-8")) != _stable(
                    produced
                ):
                    stale.append(target.relative_to(REPO_ROOT))
            else:
                target.write_text(produced, encoding="utf-8")
                print(f"wrote {target.relative_to(REPO_ROOT)}")

    if stale:
        listing = "\n  ".join(str(path) for path in stale)
        print(
            f"These dumps no longer match what the extractor produces:\n  {listing}\n"
            "Run `just corpus-dumps` and commit the result.",
            file=sys.stderr,
        )
        return 1
    if args.check:
        print("corpus dumps are current")
    return 0


def _stable(text: str) -> object:
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
