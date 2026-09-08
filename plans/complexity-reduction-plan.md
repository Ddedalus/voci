# Complexity reduction: `voci-migrate`'s densest functions

Internal working document. Found by a line-count and `ruff --select C901` sweep of `voci/` and
`voci-migrate/` (2026-09-07): the general lint set (E, F, I, UP, B, SIM, RUF) and pyrefly are both
clean, so this isn't broad debt — it's complexity concentrated in a handful of functions, all in
`voci-migrate`. File length itself isn't the problem: the two longest files
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
- PR 3 — `audit/sources.py`'s `_pytest_call`, `audit/completeness.py`'s `missing`, and
  `convert/parametrize.py`'s `_uncarriable` each split as scoped below; no behavior change. The
  `C901` gate is on. Turning it on also surfaced 10 violations this plan's original sweep had
  missed (2 more in `voci-migrate`, plus — contrary to this doc's original claim that `voci/`
  was clean — 8 in `voci/` itself); each is `# noqa: C901`'d at its definition rather than fixed
  here, tracked as [PR 4](#pr-4-the-10-violations-pr-3-found-and-noqad).

---

## PR 4 — the 10 violations PR 3 found and noqa'd

Each was `# noqa: C901`'d in place rather than fixed in PR 3, to keep that PR's review small and
because several of these are real refactors, not small splits. The gate is already on, so nothing
new can regress silently in the meantime — this PR is about paying down what's grandfathered. Each
item lands as its own PR rather than one big one, per the list below.

### Done

- `voci-migrate/voci_migrate/convert/specialize.py` `_bound_in` (11 > 10) — split the `Import`
  and `Assign` match arms' loops into `_import_bindings`/`_assign_bindings`. Under 10; no behavior
  change.

### To do

- `voci-migrate/voci_migrate/matrix.py` `_validate` (14 > 10)
- `voci/_collection/collect.py` `collect` (19 > 10)
- `voci/_di/runtime.py` `_construct` (14 > 10)
- `voci/_mocking.py` `patching_of` (11 > 10)
- `voci/_report/terminal.py` `TerminalReporter.finish` (11 > 10)
- `voci/_run/run.py` `_run_one` (31 > 10) and `run_suite` (40 > 10) — the two largest by far;
  likely each need their own PR rather than sharing one with the rest of this list.
- `voci/cli.py` `_prepare_run` (21 > 10) and `main` (28 > 10)

Regression nets: whichever suite each file's own tests live under (`voci-migrate/tests/` for the
first two, `tests/` for the rest).

---

## Out of scope

- File-length-driven splits of `sources.py` or `plan.py` — considered and rejected above; both
  are already flat, and the visitor class in `sources.py` can't be split without fighting libcst's
  API.
- `voci-migrate/tests/test_convert_rules.py` and `plan.py`'s own line count — long, but tests and
  a many-small-functions module respectively aren't the same complexity problem as a dense
  function, and shortening either isn't proposed here.
