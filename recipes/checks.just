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

# Measure examples/02-async-library under coverage.py, as CI's coverage job does: a real suite,
# `@velox.isolated` tests included, run the way a user's project would run one.
coverage *args:
    #!/usr/bin/env bash
    set -euo pipefail
    # `--project` because the example is a project of its own, with no velox in its dependencies
    # -- the environment to run it from is this repo's.
    root="$PWD"
    cd examples/02-async-library
    uv run --project "$root" coverage run --source=relay -m velox "$@"
    uv run --project "$root" coverage report -m --fail-under=95

# Run both suites under every supported interpreter (e.g. `just checks test-all -k annotated`).
# Mirrors CI's test matrix; `just checks test` and `just check` cover the default one only.
test-all *args:
    #!/usr/bin/env bash
    set -uo pipefail
    # Every leg runs even after one fails, like CI's `fail-fast: false`: the point of running two
    # interpreters is seeing which of them a change broke, which a first failure would hide.
    selective=$#
    status=0
    leg() {
        local label="$1"; shift
        local rc=0
        "$@" || rc=$?
        # 5 is pytest's "nothing collected", which a selector meant for the other suite reaches
        # honestly. Still a failure for an unfiltered run, where an empty suite is a real one.
        if [ "$rc" -eq 5 ] && [ "$selective" -gt 0 ]; then rc=0; fi
        if [ "$rc" -ne 0 ]; then echo "✗ $label"; status=1; fi
    }
    for version in $(just py versions); do
        echo "== Python $version =="
        # `--quiet` because a nested just would otherwise report a failing leg twice, once as its
        # own "recipe failed" line and once as the `✗` below; pytest's output already says what
        # went wrong.
        leg "velox tests on $version"   just --quiet py run "$version" pytest "$@"
        leg "migrate tests on $version" just --quiet py run "$version" pytest velox-migrate/tests -n auto "$@"
    done
    exit "$status"

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
