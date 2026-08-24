set positional-arguments
set working-directory := '..'

# Serve the docs site with live reload (e.g. `just docs serve --open`)
serve *args:
    uv run zensical serve "$@"

# Build the docs site into site/
build *args:
    uv run zensical build "$@"

# Regenerate docs/reference/cli.md from `velox --help` (`just docs cli --check` to verify only)
cli *args:
    uv run python scripts/gen_cli_reference.py "$@"

# Verify the site builds and that reference/cli.md still matches `velox --help`. Builds clean:
# Zensical's incremental cache keys on Markdown content, so a reference page whose only input
# changed -- a docstring in velox/ -- would otherwise be served from the previous render.
check:
    uv run python scripts/gen_cli_reference.py --check
    uv run zensical build --clean
