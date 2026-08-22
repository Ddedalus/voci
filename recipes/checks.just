set positional-arguments
set working-directory := '..'

# Lint with ruff (e.g. `just checks lint --fix`)
lint *args:
    uv run ruff check "$@" .

# Check formatting without writing changes
fmt-check:
    uv run ruff format --check .

# Type-check with pyrefly. velox-migrate has its own pyproject.toml, which pyrefly reads as a
# separate project and leaves out of the root one, so its sources are named explicitly.
typecheck *args:
    uv run pyrefly check --progress-bar no "$@"
    uv run pyrefly check --progress-bar no "$@" velox-migrate/velox_migrate velox-migrate/scripts velox-migrate/tests

# Run the test suite (e.g. `just checks test -k cli`)
test *args:
    uv run pytest "$@"

# Run lint, format-check, typecheck and tests together (quiet on success)
all:
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
    run lint       just checks lint
    run format     just checks fmt-check
    run typecheck  just checks typecheck
    run tests      just checks test
    run migrate    just migrate test
