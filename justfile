set positional-arguments

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

# Run the velox-migrate test suite (e.g. `just test-migrate -k extractor`)
test-migrate *args:
    uv run pytest velox-migrate/tests "$@"

# Regenerate the checked-in corpus ground-truth dumps, one per supported pytest
corpus-dumps *args:
    uv run python velox-migrate/scripts/refresh_corpus_dumps.py "$@"

# Verify the checked-in corpus dumps still match what the extractor produces
corpus-check:
    uv run python velox-migrate/scripts/refresh_corpus_dumps.py --check

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

# Re-vendor pytest's assertion subsystem from the pytest/ submodule (spec/07)
vendor:
    uv run python scripts/vendor_assertion.py

# Verify the vendored tree matches what the vendoring script generates
vendor-check:
    uv run python scripts/vendor_assertion.py --check

# Assert the assertion-rewrite cold/warm ratio is still within budget (spec/07 §5)
bench-cold-start *args:
    uv run python scripts/bench_cold_start.py "$@"

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
    run migrate    just test-migrate

# Refactor tools (summarize, move, rewire, init) — see refactor.justfile
import 'refactor.justfile'
