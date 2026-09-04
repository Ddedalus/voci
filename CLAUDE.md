# velox

Two distributions in this uv workspace: `velox/` (the package) and `velox-migrate/` (pytest→velox
migration tooling).

## Tooling
uv, ruff, pyrefly, pytest, run via `justfile` (`just list`). Default to `just check` to verify a change is done.

## Python versions

velox supports 3.13 and 3.14, and they differ where it matters (PEP 649 annotations, warning
filters). `.python-version` pins the default — 3.14 — and everything (`just check`, `just checks
test`, CI's non-matrix jobs) runs under it unless told otherwise.

- `just checks test-all` — both suites under every supported interpreter, as CI's matrix does.
- `just py run 3.13 <cmd>` — any command under another interpreter, e.g.
  `just py run 3.13 pytest -k annotated` or `just py run 3.13 pyrefly check` (worth doing after
  touching a `sys.version_info` branch, since a typecheck only sees the half its interpreter picks).
- `just py sync 3.13` — build that interpreter's environment up front. It lives in `.venv-3.13`,
  next to the default `.venv`, so switching back and forth never rebuilds either.

Run both when the change touches annotations, warnings, or anything version-branched; the default
alone is enough otherwise.

## References
`oss/` - inspiration repo closes
`spec/` - AI slop description of functionality
`plans/` — AI work plans

## Documentation

User docs are built in `docs`.
See the `velox-docs` skill for conventions on prose in code. 

## Workflows

If you're top-level agent, load the `dev-workflow` skill to pick git strategy. Inform subagents where they ought to work, so they don't need the skill.