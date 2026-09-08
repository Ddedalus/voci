# velox-migrate: findings from the high-effort correctness review

Eight-angle `/code-review high` of the dependency-typing merge (`25b6561`), scoped to
`velox-migrate/` and read for data loss or incorrect conversion. Two confirmed correctness bugs
were fixed directly at the time; the reuse/simplification/efficiency/altitude cleanups the same
review surfaced were fixed in a later pass. The two lower-confidence correctness gaps left after
that — `_takes()`'s missing request-forwarding case and `_absolute()`'s off-by-one — are fixed too,
each with a regression test; see `audit/sources.py`, `convert/rules/imports.py` and their tests
rather than this document for the detail. `audit/sources.py`'s prefix-based test recognition, the
other half of the `_takes()` fix, also closed the altitude note below about a `test_*`-named method
on a class `python_classes` would not collect: a dump-backed scan now reads `ground_truth.items`
instead of guessing, so a class the dump never collected contributes no false positive. The
forwarding fix follows a chain of same-file helpers to its end and does not conflate two functions
sharing a name in different scopes, but stays file-local by design: a helper imported from a
sibling module keeps its `request` parameter unrecognized, the same conservative direction the rest
of this heuristic already takes.

## Altitude — not urgent, revisit before a second plugin of the kind

- `wiring.py`'s `_backend_findings`/`_backend_name` (VX324) key detection of "parametrized onto a
  non-asyncio backend" off exactly the `anyio_backend` callspec parameter and the literal string
  `"asyncio"` — a different async plugin exposing an equivalently-purposed fixture under another
  name produces no finding. Not urgent — it degrades an audit finding's precision, not conversion
  output.

---

See [ROADMAP.md](../ROADMAP.md) § Code quality consolidation.
