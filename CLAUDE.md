# velox

Package: `velox/`, nested by subsystem (`_di/`, `_collection/`, `_run/`, `_report/`,
`_builtins/`, `_assertions/`). `__init__.py`, `_version.py`, `cli.py`, `fastapi.py`, `_config.py`
and `_marks.py` stay flat at the package root — `fastapi.py`'s import path (`velox.fastapi`) is
public, and the rest have no roadmap growth pointing at a split. Tests: `tests/`, mirroring the
package (`tests/di/`, `tests/collection/`, ...); a test file that only exercises the public
`velox` surface, not a subpackage's internals, stays flat. Entrypoint: `velox.cli:main`.

Tooling: uv, ruff, pyrefly, pytest. Run via `justfile` — `just list` for recipes (`sync`, `run`,
`test`, `lint`, `fmt`, `typecheck`, `build`, `check`). For splitting objects out of a file into
their own module and repointing imports, see the `refactor-tools` skill.

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
