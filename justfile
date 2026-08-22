set positional-arguments

mod bench 'recipes/bench.just'
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

# Run the test suite (e.g. `just test -k cli`)
test *args:
    uv run pytest "$@"

# Lint with ruff (e.g. `just lint --fix`)
lint *args:
    uv run ruff check "$@" .

# Format with ruff
fmt *args:
    uv run ruff format "$@" .

# Check formatting without writing changes
fmt-check:
    uv run ruff format --check .

# Type-check with pyrefly. velox-migrate has its own pyproject.toml, which pyrefly reads as a
# separate project and leaves out of the root one, so its sources are named explicitly.
typecheck *args:
    uv run pyrefly check --progress-bar no "$@"
    uv run pyrefly check --progress-bar no "$@" velox-migrate/velox_migrate velox-migrate/scripts velox-migrate/tests

# Build the package (sdist + wheel)
build:
    uv build

# Run lint, format-check, typecheck and tests together (quiet on success)
check:
    #!/usr/bin/env bash
    set -euo pipefail
    run() {
        local desc="$1"; shift
        local out status=0
        out=$("$@" 2>&1) || status=$?
        if [ "$status" -eq 0 ]; then
            echo "✓ $desc"
        else
            echo "✗ $desc"
            printf '%s\n' "$out"
            exit "$status"
        fi
    }
    run lint       just lint
    run format     just fmt-check
    run typecheck  just typecheck
    run tests      just test
    run migrate    just migrate test
