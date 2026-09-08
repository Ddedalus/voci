# voci

The package, nested by subsystem (`_di/`, `_collection/`, `_run/`, `_report/`, `_builtins/`,
`_assertions/`). `__init__.py`, `_version.py`, `cli.py`, `fastapi.py`, `_config.py` and
`_marks.py` stay flat at the package root — `fastapi.py`'s import path (`voci.fastapi`) is
public, and the rest have no roadmap growth pointing at a split. Tests live in `tests/`, mirroring
this package (`tests/di/`, `tests/collection/`, ...); a test file that only exercises the public
`voci` surface, not a subpackage's internals, stays flat. Entrypoint: `voci.cli:main`.

`_assertions/_vendor/` is **generated** from the `oss/pytest/` submodule — never edit it by hand.
Regenerate with `just vendor update` (`scripts/vendor_assertion.py`, which logs every edit in
`_assertions/VENDOR.md`); `just vendor check` verifies the tree is current. Kept byte-identical to
upstream and excluded from ruff and pyrefly, except the hand-written `_shim.py`.

For splitting objects out of a file into their own module and repointing imports, see the
`refactor-tools` skill.

voci never depends on `voci-migrate` — see that distribution's own CLAUDE.md.
