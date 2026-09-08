set positional-arguments
set working-directory := '..'

# Both interpreters voci supports. They disagree about annotations (PEP 649: on 3.14 they are
# computed lazily and reach `voci._di.fixtures` as source text, where 3.13 hands it objects), so
# each version exercises a different half of the injection scan and of the `sys.version_info`
# branches around it. `.python-version` picks the one everything runs under by default.
#
# CI can't read this list -- its workflow calls uv directly, without just -- so the `python-version`
# matrices in `.github/workflows/ci.yml` spell out the same versions and have to be edited with it.
supported := "3.13 3.14"

# The supported interpreters, one per line (`just checks test-all` iterates over this)
versions:
    @printf '%s\n' {{ supported }}

# Sync the environment for one interpreter (e.g. `just py sync 3.13`)
sync version:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ "$1" = "$(cat .python-version)" ]; then env=.venv; else env=".venv-$1"; fi
    UV_PYTHON="$1" UV_PROJECT_ENVIRONMENT="$env" uv sync --all-packages

# Run a command under one interpreter (e.g. `just py run 3.13 pytest -k annotated`)
run version *args:
    #!/usr/bin/env bash
    set -euo pipefail
    version="$1"; shift
    # The default interpreter keeps `.venv`, every other version gets its own `.venv-X.Y`.
    # Pointing both at `.venv` instead would make uv tear the environment down and rebuild it on
    # every switch, which is slow enough that agents stop switching.
    if [ "$version" = "$(cat .python-version)" ]; then env=.venv; else env=".venv-$version"; fi
    # `--all-packages` because uv's implicit sync would otherwise install the root project alone
    # and leave `voci_migrate` missing from a fresh alternate environment.
    UV_PYTHON="$version" UV_PROJECT_ENVIRONMENT="$env" uv run --all-packages "$@"
