set positional-arguments
set working-directory := '..'

# Serve the docs site with live reload (e.g. `just docs serve --open`)
serve *args:
    uv run zensical serve "$@"

# Build the docs site into site/
build *args:
    uv run zensical build "$@"
