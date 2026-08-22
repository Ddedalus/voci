# velox-migrate

Second distribution in this uv workspace (dist `velox-migrate`, import `velox_migrate`), holding
the pytest→velox migration tooling. velox never depends on it. Tests are in `tests/`; run with
`just migrate test`.

`velox_migrate/extractor.py` is a single file importing only stdlib and pytest so it can be copied
into an environment where nothing else can be installed — keep it that way. Everything else here
may use LibCST, its one dependency. `velox_migrate/matrix.py` is the support matrix: one row per
pytest construct, keyed by a `VXnnn` code that report sections, `VELOX-TODO` markers and rewrite
rules all reconcile against — classification decisions belong in that table, not in the code that
reads it.

`corpus/` holds pytest suites that exist to be extracted from and converted rather than run
directly, so it's excluded from ruff and pyrefly. `corpus/dumps/` holds their checked-in
ground-truth dumps, one per supported pytest — regenerate with `just migrate corpus-dumps`, verify
with `just migrate corpus-check`.
