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

# Run the test suite under the default interpreter (e.g. `just checks test -k cli`)
test *args:
    uv run pytest "$@"

# Run both suites under every supported interpreter (e.g. `just checks test-all -k annotated`).
# Mirrors CI's test matrix; `just checks test` and `just check` cover the default one only.
test-all *args:
    #!/usr/bin/env bash
    set -euo pipefail
    for version in $(just py versions); do
        echo "== Python $version =="
        just py run "$version" pytest "$@"
        just py run "$version" pytest velox-migrate/tests -n auto "$@"
    done

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
