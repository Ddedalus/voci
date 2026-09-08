# Complexity reduction: `velox-migrate`'s densest functions

Internal working document. Found by a line-count and `ruff --select C901` sweep of `velox/` and
`velox-migrate/` (2026-09-07): the general lint set (E, F, I, UP, B, SIM, RUF) and pyrefly are both
clean, so this isn't broad debt — it's complexity concentrated in a handful of functions, all in
`velox-migrate`. File length itself isn't the problem: the two longest files
(`audit/sources.py`, `convert/plan.py`) are already flat collections of small functions (or, for
`sources.py`, one libcst visitor class that has to be a single class by libcst's own API). No file
split is proposed here — each PR below simplifies functions in place.

`C901` is not currently in `pyproject.toml`'s `[tool.ruff.lint].select`, so none of this trips
`just check` today. The last PR below turns the gate on.

## Done

- PR 1 — `convert/rules/marks.py`'s `_MarkPass` split into smaller per-mark helpers (`translate`,
  `_xfail`, `_parametrize`, and `_unwrap` — the sweep found `_unwrap` over threshold too, not
  named in the original PR 1 scope below). All four under 10; no behavior change.
- PR 2 — `convert/plan.py`'s `_propagate` and `_consumers` each split into one helper per
  sub-decision (`_propagate_edges`/`_propagate_deps`/`_needs_request`,
  `_consumers_from_tests`/`_consumers_from_fixtures`/`_consumers_from_copies`/`_add_requested`).
  Both under 10; no behavior change.

---

## PR 3 — small remaining spots, then turn on the `C901` gate

Three independent, small items — batched into one PR because none is worth its own review round:

- [audit/sources.py:455](../velox-migrate/velox_migrate/audit/sources.py#L455) `_pytest_call`
  (14 > 10) — one dispatch method on `_Scanner` (otherwise a well-organized visitor; leave the
  rest of the class alone) covering every recognized `pytest.*` call shape. Split by the API
  being recognized (`pytest.raises`, `pytest.warns`, `pytest.approx`, etc.), same pattern as the
  file's other `_xxx_call` helpers.
- [audit/completeness.py:82](../velox-migrate/velox_migrate/audit/completeness.py#L82) `missing`
  (11 > 10).
- [convert/parametrize.py:310](../velox-migrate/velox_migrate/convert/parametrize.py#L310)
  `_uncarriable` (13 > 10).

Regression nets: `test_audit_sources.py`, `test_audit_completeness.py`,
`test_convert_parametrize.py` respectively.

Once all three are under threshold, add `"C901"` to `[tool.ruff.lint].select` in `pyproject.toml`
(default max-complexity is 10, matching what this whole plan was measured against) so a future
regression here shows up in `just check` instead of needing another ad-hoc sweep.

Verify: `just checks test`, then `just check` (confirm the new `C901` select passes clean before
committing it).

---

## Out of scope

- File-length-driven splits of `sources.py` or `plan.py` — considered and rejected above; both
  are already flat, and the visitor class in `sources.py` can't be split without fighting libcst's
  API.
- `velox-migrate/tests/test_convert_rules.py` and `plan.py`'s own line count — long, but tests and
  a many-small-functions module respectively aren't the same complexity problem as a dense
  function, and shortening either isn't proposed here.
