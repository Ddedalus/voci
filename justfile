set positional-arguments

mod bench 'recipes/bench.just'
mod checks 'recipes/checks.just'
mod docs 'recipes/docs.just'
mod migrate 'recipes/migrate.just'
mod refactor 'recipes/refactor.just'
mod vendor 'recipes/vendor.just'

# List available recipes
default: list

# List available recipes
list:
    @just --list

# Install/sync the dev environment (both workspace members, editable)
sync:
    uv sync --all-packages

# Run the velox CLI (e.g. `just run --version`)
run *args:
    uv run velox "$@"

# Format with ruff
fmt *args:
    uv run ruff format "$@" .

# Build the package (sdist + wheel)
build:
    uv build

# Run lint, format-check, typecheck and tests together (quiet on success)
check:
    just checks all
