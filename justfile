set positional-arguments

# Assertion-rewrite cold/warm benchmark
mod bench 'recipes/bench.just'

# check's dependents (lint, fmt-check, typecheck, test), also runnable standalone
mod checks 'recipes/checks.just'

# Docs site (zensical)
mod docs 'recipes/docs.just'

# velox-migrate tests and corpus dumps
mod migrate 'recipes/migrate.just'

# Run against a non-default interpreter (velox supports 3.13 and 3.14)
mod py 'recipes/py.just'

# AST-based tools for splitting/moving top-level objects and rewiring their imports
mod refactor 'recipes/refactor.just'

# Vendored pytest assertion subsystem (oss/pytest submodule)
mod vendor 'recipes/vendor.just'

# List available recipes
default: list

# List available recipes
list:
    @just --list

# Install/sync the dev env for the default interpreter (`just py sync X.Y` for the others)
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
