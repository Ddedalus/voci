# pytest → velox migration tool: implementation plan

# Tasks

Ordered by risk retired per unit of work; each phase has a checkable exit.

- [x] **Phase 0 — extractor + schema.**
- [x] **Phase 1 — audit.**
- [x] **Phase 2 — mechanical convert.** 
- [x] **Phase 3 — the hard §4 machinery.** Declaration placement (autouse/`usefixtures`), fixture-graph
  specialization within budget, `request` elimination, and `mock.patch` handling all convert across seven
  corpus suites with nothing refused (over-budget chains refuse with correct fan-out); `plan.DEFERRED` is
  empty.
- **Phase 4 — apply against real test suites**
See [httpx2-audit.md](httpx2-audit.md) and [oss-refactors-plan.md](oss-refactors-plan.md)
- **Phase 6 - skills and prefactors** - to be scoped


## Decisions summary

* velox itself grows no override mechanism
* the migration tool is a separate distribution — the velox runtime stays minimal.

| Question | Decision |
|---|---|
| Pipeline | `extract → audit → convert → verify`, artifact-coupled through a `.velox-migrate/` work dir |
| §11 Q2: preserve conftest layout or consolidate? | Preserve: one fixture module per directory that had a `conftest.py`. Consolidation is a post-migration cleanup skill. |
| §11 Q3: specialization budget | Per-override fan-out budget; over budget → loud refusal + pointer to the unwind-override prefactor skill |
| §11 Q5: propose DI seams? | Report the opportunity (audit) and assist the refactor (skill); `convert` never does it |
| §11 Q6: is `--concurrency 1` green a tool-enforced gate? | A subcommand (`verify`), strongly recommended in the workflow, not a hard gate — it requires both runners runnable in one env, which is not always true |

# Planned scope 
## The pipeline

Four subcommands.

**`extract`** → `ground-truth.json`. Runs in the suite's environment; the only stage that needs pytest.
**`audit`** → `migration-report.md` + `findings.json` + terminal summary. Classifies every construct against support matrix.
**`convert`** — the deterministic codegen. Dry-run is the default, `--write` is explicit.
**`verify`** — runs pytest on the pre-migration tree and `velox --serial` on the converted tree,


## Where AI fits: prefactor and postfactor, never convert

The organizing principle: **every rewrite that can be expressed as pytest→pytest runs before
conversion, while the suite is still green under pytest** — because there, pytest itself is the
verification oracle for each change. `convert` is reserved for the one rewrite that cannot exist
in pytest-land: the wiring swap. After conversion there is no oracle until `verify`, which is
exactly why `convert` must be deterministic, mechanical, and boring.

That gives three tiers:

1. **Prefactor codemods** (deterministic, in `prefactor/`, run under pytest): `tmpdir` →
   `tmp_path`, legacy `pytest.raises(E, fn, args)` call form → context manager, unconditional
   `request.addfinalizer` → yield form, literal `request.getfixturevalue("x")` → a parameter,
   `usefixtures` on one test in a shared module → fixture moved so the mark can become
   module-level. Each shrinks `convert`'s surface, and each is verified by the suite staying
   green.

2. **Prefactor skills** (AI-assisted, in `skills/`, driven by `findings.json`): the judgment refactors. The skill proposes and applies a refactor *in pytest terms*.

3. **Postfactor skills**: triage after `verify` at concurrency — choosing `@velox.solo` vs `@velox.isolated` vs a seam per §6 site and the optional consolidate-fixtures cleanup for teams that want the idiomatic layout after the reviewable diff has landed.

## 6. Override chains without an override mechanism (§4.2)

With no override feature in velox, a conftest override has two translations:

- **Within budget**: `specialize.py` generates the specialized chain — the overriding fixture plus a copy of every fixture strictly between it and each test that resolves through it, with new names.
- **Over budget**: refuse loudly. The audit reports each override's fan-out , pointing at the unwind-override skill.

The budget's unit is *generated fixtures per override scope*, default≈5.

# References
Mainly historical:
 * [migration-problem-statement.md](migration-problem-statement.md) (cited below by §).
 * [research/migration/codegen-libraries.md](../research/migration/codegen-libraries.md) and
 * [research/migration/pytest-ground-truth.md](../research/migration/pytest-ground-truth.md).
