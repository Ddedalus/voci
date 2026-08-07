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
    uv run pyrefly check "$@"

# Build the package (sdist + wheel)
build:
    uv build

# Run lint, format-check, typecheck and tests together
check: lint fmt-check typecheck test
