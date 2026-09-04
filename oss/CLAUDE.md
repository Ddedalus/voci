# oss

Read-only checkouts of upstream projects as git submodules (see `.gitmodules`), reference-only —
excluded from ruff and pyrefly, never edited by hand. `oss/pytest` is also what `just vendor
update` re-vendors `velox/_assertions/_vendor/` from. `oss/flask` and `oss/marshmallow` are two
of the Phase 4 migration corpus suites — see `plans/migration-findings.md`.
