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

---

## PR 1 — `convert/rules/marks.py`: break up `_MarkPass`

[convert/rules/marks.py:86-704](../velox-migrate/velox_migrate/convert/rules/marks.py#L86-L704)
is one 618-line class holding the translate-this-mark logic for every `pytest.mark.*` kind.
Three of its methods are the worst complexity offenders in the whole codebase:

- `translate` (11 > 10) — the `mark.name ==` dispatch chain
- `_xfail` (13 > 10)
- `_parametrize` (13 > 10)

Work: pull each mark kind's translation logic (already delimited by `# --- xxx ---` comment
banners in the file) into smaller named helpers so no single method's branching covers more than
one mark shape at a time. Keep `_MarkPass` as the shared home for `mark_of`/`translate` dispatch;
the per-mark bodies are the ones to shrink. No behavior change — `velox-migrate/tests/
test_convert_rules.py` (2407 lines, already exercises every mark kind) is the regression net; add
cases there only if the split reveals a branch it doesn't already cover.

Verify: `just checks test` for velox-migrate, then `just check`.

## PR 2 — `convert/plan.py`: simplify the fixture-propagation pair

[convert/plan.py:855](../velox-migrate/velox_migrate/convert/plan.py#L855) `_propagate` (13 > 10)
and [convert/plan.py:981](../velox-migrate/velox_migrate/convert/plan.py#L981) `_consumers`
(15 > 10) are the two most complex functions in the fixture-graph planning module. Unlike PR 1
these aren't one class — they're free functions the module already keeps small elsewhere, so the
fix is standard extract-helper-function work: name the sub-decisions each one currently inlines
(e.g. whatever `_consumers` branches on 15 ways) and pull them out.

Read `_propagate` and `_consumers` fresh before starting — this plan doesn't prescribe the split,
since the right seams depend on what the two functions are actually branching on.

Regression net: `velox-migrate/tests/test_convert_rules.py` and `test_convert.py` both exercise
`plan.build()`; confirm which cases actually reach `_propagate`/`_consumers` before relying on
them, and add direct cases if the existing coverage turns out to be indirect only.

Verify: `just checks test` for velox-migrate, then `just check`.

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
