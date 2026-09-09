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
of this heuristic already takes.

## Correctness — data loss, found incidentally by a misscoped review pass

- `convert/parametrize.py`'s `_row_index` (added by `8063807`, the VC114 stacked-direct-param fix)
  keys each spec's recovered position on the `repr` of its whole value row
  (`seen.setdefault(tuple(spec.params.get(name, "") for name in argnames), len(seen))`), not on
  pytest's own per-case index. Two distinct declared cases whose values happen to repr equal —
  `@pytest.mark.parametrize("flag", [True, False, True])`, case 0 and case 2 both rowing to
  `('True',)` — collapse onto the same synthetic position, so `_values` (which trusts that index)
  silently drops the repeated case's data instead of emitting three values. Direct repro: calling
  `axes()` on three such synthetic specs returns two `Axis` values instead of three. No test in the
  VC114 diff or the corpus exercises a parametrize list with a repeated value, so `just migrate
  test` passes despite it. Needs a fix that only falls back to value-based row identity for the
  axes actually affected by the direct-param index fold, not unconditionally for every axis
  (`_values`/`_position_of` too, both downstream of the same `index` tuple).

## Altitude — not urgent, revisit before a second plugin of the kind

- `wiring.py`'s `_backend_findings`/`_backend_name` (VC324) key detection of "parametrized onto a
  non-asyncio backend" off exactly the `anyio_backend` callspec parameter and the literal string
  `"asyncio"` — a different async plugin exposing an equivalently-purposed fixture under another
  name produces no finding. Not urgent — it degrades an audit finding's precision, not conversion
  output.

---

See [ROADMAP.md](../ROADMAP.md) § Code quality consolidation.
