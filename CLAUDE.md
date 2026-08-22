# velox

Two distributions in this uv workspace: `velox/` (the package) and `velox-migrate/` (pytest→velox
migration tooling).

## Tooling
uv, ruff, pyrefly, pytest, run via `justfile` (`just list`). Default to `just check` to verify a change is done.

## References
`oss/` - inspiration repo closes
`spec/` - AI slop description of functionality
`plans/` — AI work plans

## Documentation

User docs are built in `docs`.
See the `velox-docs` skill for conventions on prose in code. 

## Workflows

If you're top-level agent, load the `dev-workflow` skill to pick git strategy. Inform subagents where they ought to work, so they don't need the skill.