set positional-arguments
set working-directory := '..'

# Serve the docs site with live reload (e.g. `just docs serve --open`)
serve *args:
    uv run zensical serve "$@"

# Build the docs site into site/
build *args:
    uv run zensical build "$@"

# Regenerate docs/reference/cli.md from `voci --help` (`just docs cli --check` to verify only)
cli *args:
    uv run python scripts/gen_cli_reference.py "$@"

# Regenerate docs/migrate/matrix.md from the support matrix (`--check` to verify only)
matrix *args:
    uv run python scripts/gen_support_matrix.py "$@"

# Verify the site builds and that the generated pages still match their sources. Builds clean:
# Zensical's incremental cache keys on Markdown content, so a reference page whose only input
# changed -- a docstring in voci/ -- would otherwise be served from the previous render.
check:
    uv run python scripts/gen_cli_reference.py --check
    uv run python scripts/gen_support_matrix.py --check
    uv run zensical build --clean
