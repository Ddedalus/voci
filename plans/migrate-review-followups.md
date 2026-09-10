# voci-migrate: findings from the high-effort correctness review

Eight-angle `/code-review high` of the dependency-typing merge (`25b6561`), scoped to
`voci-migrate/` and read for data loss or incorrect conversion. Two confirmed correctness bugs
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
of this heuristic already takes. A third correctness bug, found incidentally by a misscoped review
pass — `convert/parametrize.py`'s `_row_index` collapsing repeated-value cases onto one position —
is fixed too; see `axes()`/`_direct_names()`/`_row_index()` and
`test_a_repeated_value_keeps_its_own_case` in `tests/test_convert_parametrize.py`. A follow-up
review of that fix found `_direct_names()` only recognised the direct-param fold for a
mark-covered name, missing the identical fold pytest applies to a `pytest_generate_tests` hook
parametrizing a plain name with no fixture behind it; closed the same way, covered by
`test_a_hook_axis_stacked_with_a_mark_stays_its_own_axis`.

## Altitude — not urgent, revisit before a second plugin of the kind

- `wiring.py`'s `_backend_findings`/`_backend_name` (VC324) key detection of "parametrized onto a
  non-asyncio backend" off exactly the `anyio_backend` callspec parameter and the literal string
  `"asyncio"` — a different async plugin exposing an equivalently-purposed fixture under another
  name produces no finding. Not urgent — it degrades an audit finding's precision, not conversion
  output.

- A test stacking a `pytest_generate_tests`-built axis with a `@pytest.mark.parametrize` one
  (`axes()`'s two "direct" kinds together, the shape the hook-fold fix above now reads correctly)
  refuses to convert: `convert --write` emits a plain function parameter for the hook-built name
  instead of wiring it through `@voci.parametrize`, so the written test has an uninjected
  parameter and fails collection under voci. Found while adding a corpus regression case for the
  fix above and reverted out of the corpus rather than chased here — no real suite in the corpus
  or the two real runs (`migration-findings.md`) has this shape yet, so it's a latent gap rather
  than a live one. Whoever picks it up next should look at `wiring.py`'s `Generated`-vs-`MARK`
  handling for a site with axes of both kinds.

---

See [ROADMAP.md](../ROADMAP.md) § Code quality consolidation.
