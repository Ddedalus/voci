# velox

Package: `velox/`, nested by subsystem (`_di/`, `_collection/`, `_run/`, `_report/`,
`_builtins/`, `_assertions/`). `__init__.py`, `_version.py`, `cli.py`, `fastapi.py`, `_config.py`
and `_marks.py` stay flat at the package root — `fastapi.py`'s import path (`velox.fastapi`) is
public, and the rest have no roadmap growth pointing at a split. Tests: `tests/`, mirroring the
package (`tests/di/`, `tests/collection/`, ...); a test file that only exercises the public
`velox` surface, not a subpackage's internals, stays flat. Entrypoint: `velox.cli:main`.

Second distribution: `velox-migrate/` (dist `velox-migrate`, import `velox_migrate`), a uv
workspace member holding the pytest→velox migration tooling. velox never depends on it. Its
tests are `velox-migrate/tests/`; `velox-migrate/corpus/` holds pytest suites that exist to be
extracted from rather than run, so it is excluded from ruff and pyrefly, and
`velox-migrate/corpus/dumps/` holds their checked-in ground-truth dumps, one per supported pytest
— regenerate with `just corpus-dumps`, verify with `just corpus-check`. `velox_migrate/extractor.py`
is a single file importing only stdlib and pytest so it can be copied into an environment where
nothing else can be installed; keep it that way. Everything else there may use LibCST, its one
dependency. `velox_migrate/matrix.py` is the support matrix: one row per pytest construct, keyed by
a `VXnnn` code that report sections, `VELOX-TODO` markers and rewrite rules all reconcile against —
classification decisions belong in that table, not in the code that reads it.

Tooling: uv, ruff, pyrefly, pytest. Run via `justfile` — `just list` for recipes (`sync`, `run`,
`test`, `test-migrate`, `lint`, `fmt`, `typecheck`, `build`, `check`). For splitting objects out
of a file into their own module and repointing imports, see the `refactor-tools` skill.

Reference-only, not part of the package: `pytest/`, `fastapi/`, `research/`, `spec/`. `pytest/`
and `fastapi/` are git submodules. Never cite `spec/` outside `spec/` — it's a scratch design
artifact, not published, and will be deleted.

`velox/_assertions/_vendor/` is **generated** from the `pytest/` submodule — never edit it by
hand. Regenerate with `just vendor` (`scripts/vendor_assertion.py`, which logs every edit in
`velox/_assertions/VENDOR.md`); `just vendor-check` verifies the tree is current. Kept
byte-identical to upstream and excluded from ruff and pyrefly, except the hand-written `_shim.py`.

## Documentation

Docs are a product surface, not a build log: write for a reader who knows nothing about how velox
was built. `README.md` sells and onboards; `ROADMAP.md` is the only place unbuilt behavior is
described; `docs/rationale.md` holds the WHY behind decisions; docstrings state what a thing is
plus only what a caller would get wrong otherwise. No counterfactuals ("doesn't yet", "used to",
"instead of"), no milestone/spec vocabulary outside `spec/`, no status boasts. See the
`velox-docs` skill for the full layer table, rules, and worked examples before writing or
editing any doc, docstring, or comment.

# Workflows
This is a prototype repo not yet published. We're working alone locally. There is no human code review, just fast AI iteration.

Pick the appropriate workflow for each session.

## Direct commit workflow

When asked to work on docs, roadmap and other admin or plans, commit directly to current branch, including main. Also when asked to fix CI or tests where job is reasonably small.

## Worktree workflow

When asked to do work on feature or larger refactor:
1. Create a worktree off main
2. Do the work. Commit.
3. Spawn /code-review <level> <branch> (default: medium, hard for very complex changes)
4. Address all findings. Commit.
5. Clean up docs roadmap etc.
6. Merge into main. Delete the worktree.

If **material** uncertainty exists after plan or implementation, explain and only merge once clarified.
When asked to implement roadmap item, delete its entry from ROADMAP.md as part of the worktree.

## Branch workflow

Only branch in current worktree if explicitly asked. Do not merge until instructed.