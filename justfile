set positional-arguments

# List available recipes
default: list

# List available recipes
list:
    @just --list

# Install/sync the dev environment
sync:
    uv sync

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

# Type-check with pyrefly
typecheck *args:
    uv run pyrefly check --progress-bar no "$@"

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

# Run lint, format-check, typecheck and tests together
check: lint fmt-check typecheck test

# Refactor tools (summarize, move, rewire, init) — see refactor.justfile
import 'refactor.justfile'
