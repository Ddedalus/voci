# velox

Package: `velox/` (flat layout). Tests: `tests/`. Entrypoint: `velox.cli:main`.

Tooling: uv, ruff, pyrefly, pytest. Run via `justfile` — `just list` for recipes
(`sync`, `run`, `test`, `lint`, `fmt`, `typecheck`, `build`, `check`).

Reference-only, not part of the package: `pytest/`, `fastapi/`, `research/`, `spec/`.
Git submodules for reference: `pytest/`, `fastapi/`.

`velox/_vendor/assertion/` is **generated** from the `pytest/` submodule — never edit it by
hand. Regenerate with `just vendor` (`scripts/vendor_assertion.py`, which records every edit
in `velox/_vendor/VENDOR.md`); `just vendor-check` verifies the tree is current. It is kept
byte-identical to upstream and excluded from ruff and pyrefly. The one hand-written file there
is `_shim.py`.
