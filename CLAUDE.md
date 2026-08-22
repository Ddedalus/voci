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
extracted from and converted rather than run directly, so it is excluded from ruff and pyrefly, and
`velox-migrate/corpus/dumps/` holds their checked-in ground-truth dumps, one per supported pytest
— regenerate with `just migrate corpus-dumps`, verify with `just migrate corpus-check`. `velox_migrate/extractor.py`
is a single file importing only stdlib and pytest so it can be copied into an environment where
nothing else can be installed; keep it that way. Everything else there may use LibCST, its one
dependency. `velox_migrate/matrix.py` is the support matrix: one row per pytest construct, keyed by
a `VXnnn` code that report sections, `VELOX-TODO` markers and rewrite rules all reconcile against —
classification decisions belong in that table, not in the code that reads it.

Tooling: uv, ruff, pyrefly, pytest. Run via `justfile` — `just list` for root recipes (`sync`,
`run`, `fmt`, `build`, `check`). Recipes not central to the daily dev loop live in
`recipes/<module>.just` and are invoked `just <module> <recipe>` — `checks` (lint, fmt-check,
typecheck, test: `check`'s dependents, also runnable standalone), `migrate` (velox-migrate tests
and corpus dumps), `docs`, `vendor`, `bench`, `refactor`; `just --list <module>` lists a module's
recipes. For splitting objects out of a file into their own module and repointing imports, see
the `refactor-tools` skill.

Reference-only, not part of the package: `oss/`, `research/`, `spec/`. `oss/` holds other
projects' checkouts as git submodules — `oss/pytest` and `oss/fastapi` among them — and is
excluded from ruff and pyrefly. Never cite `spec/` outside `spec/` — it's a scratch design
artifact, not published, and will be deleted.

`plans/` holds internal working documents — research syntheses and implementation plans, written
for whoever is building the thing rather than for a user. Milestone vocabulary, spec citations,
and "internal working document" framing belong there, not in `docs/`. Files there cross-reference
each other, so a link to another file in `plans/` stays a bare filename, not a `plans/`-prefixed
path.

`velox/_assertions/_vendor/` is **generated** from the `oss/pytest/` submodule — never edit it by
hand. Regenerate with `just vendor update` (`scripts/vendor_assertion.py`, which logs every edit in
`velox/_assertions/VENDOR.md`); `just vendor check` verifies the tree is current. Kept
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
1. Create the worktree as a sibling of the repo, not with `EnterWorktree`'s default
   `.claude/worktrees/` placement: `git worktree add ../velox-wt-<name> -b <branch>`, then
   `EnterWorktree(path: "/home/hubert/velox-wt-<name>")` to attach the session to it. Reason:
   `.git/info/exclude` hides `**/.claude/worktrees/` from git status, and pyrefly honors that same
   file when resolving `project-includes` globs, so `just checks typecheck`'s first command finds
   zero files and fails for any worktree placed there. A sibling directory doesn't match that pattern.
   Run `just sync` once inside the new worktree before `just check` — it has its own `.venv`.
2. Do the work. Commit.
3. Spawn /code-review <level> <branch> (default: medium, hard for very complex changes)
4. Address all findings. Commit.
5. Clean up docs roadmap etc.
6. Merge into main. Delete the worktree — since it was entered via `EnterWorktree(path:...)`
   rather than created by it, `ExitWorktree(action: "remove")` will refuse; instead
   `ExitWorktree(action: "keep")` then `git worktree remove ../velox-wt-<name>` and
   `git branch -D <branch>` from the main checkout.

If **material** uncertainty exists after plan or implementation, explain and only merge once clarified.
When asked to implement roadmap item, delete its entry from ROADMAP.md as part of the worktree.

## Branch workflow

Only branch in current worktree if explicitly asked. Do not merge until instructed.