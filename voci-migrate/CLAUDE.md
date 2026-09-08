# voci-migrate

Second distribution in this uv workspace (dist `voci-migrate`, import `voci_migrate`), holding
the pytest→voci migration tooling. voci never depends on it. Tests are in `tests/`; run with
`just migrate test`.

`voci_migrate/extractor.py` and `voci_migrate/outcomes.py` are single files importing only stdlib
and pytest so they can be copied into an environment where nothing else can be installed — keep
them that way, constants they share with the rest of the package mirrored rather than imported
(`cli.DEFAULT_OUT`, `verify.runners.DEFAULT_BASELINE`). Everything else here
may use LibCST, its one dependency. `voci_migrate/matrix.py` is the support matrix: one row per
pytest construct, keyed by a `VCnnn` code that report sections, `VOCI-TODO` markers and rewrite
rules all reconcile against — classification decisions belong in that table, not in the code that
reads it. `docs/migrate/matrix.md` is rendered from it by
`scripts/gen_support_matrix.py`, so a new or changed row needs `just docs matrix`.

`corpus/` holds pytest suites that exist to be extracted from and converted rather than run
directly, so it's excluded from ruff and pyrefly. `corpus/dumps/` holds their checked-in
ground-truth dumps, one per supported pytest — regenerate with `just migrate corpus-dumps`, verify
with `just migrate corpus-check`.
