# pytest → velox migration tool: implementation plan

The tool exists and its own suite is green (973 tests). Two real suites have been through it:
marshmallow converts and runs, httpx2 has been audited but not converted. What that turned up is in
[migration-findings.md](migration-findings.md), and it is why the remaining work below is not just
"finish Phase 4".

# Tasks

## Phase 4 — apply against real test suites

Phases 0–3 are done: extractor and schema, audit, mechanical convert, and the hard §4 machinery
(declaration placement, fixture-graph specialization within budget, `request` elimination,
`mock.patch`), all converting across the corpus suites with nothing refused.

Exit: httpx2 migrated end to end and written up.

- [x] **0. Scope httpx2.** Viable, four times the size the inherited httpx numbers said, and it
  found four audit defects on the way.
- [x] **1. `verify` subcommand.** Both runners, outcomes diffed through the id map; `outcomes.py`
  is a second copyable single-file plugin, `--record` takes the pytest half before
  `convert --write` overwrites the tree.
- [x] **2. marshmallow: convert + verify + corpus-ify.** 1183 of 1188 pass under `velox --serial`
  and the same 1183 at full concurrency. Corpus-ified as `classes_showcase`.
- [x] **3. httpx2: audit findings write-up.** 342 blocked of 1991, 88.0% serial on one autouse
  `clean_environ`, no override chains.

- [ ] **4. Make the audit report what it does not recognise.** A construct with no matrix row is
  currently counted as *converts untouched*, so the headline number cannot distinguish "nothing
  wrong with this" from "nothing looked at this" — which is how marshmallow's audit came back clean
  on four constructs that then generated uncompilable code. Add an unclassified count: a fixture,
  test body, class body or config key that no rule claimed is listed with its file and line rather
  than silently absorbed into the clean bucket.

  *Exit:* audit a suite from outside the corpus and outside the ten already scanned; the surprises
  in it land in the unclassified list rather than in the clean count.

- [ ] **5. Unwind the autouse global-state fixture.** The single thing standing between a correct
  conversion and a suite that is worth converting: httpx2 is 88% serial on `clean_environ`, flask
  and rich are the same shape, and none of it is a codegen problem. Decide, with httpx2's sites in
  front of you, which of the answers this takes — `[tool.velox] env` for what every test set
  identically, a DI seam, `@velox.solo` for the residue — and whether it lands as a deterministic
  codemod, a skill, or a documented recipe. `prefactor/` does not exist and should not be built
  speculatively; build the tier only if this rule needs it.

  httpx2's other prefactor is settled and needs no machinery: a suite-level `anyio_backend`
  returning `"asyncio"`, written by hand, pins the backend matrix and drops the `[trio]` half.

  *Exit:* httpx2's projected serial share, re-measured after the unwind, is small enough that the
  before/after concurrency number is worth publishing.

- [ ] **6. httpx2: convert + verify.** Re-run the audit first — the recorded counts predate
  `@velox.filterwarnings` and `[tool.velox] filterwarnings`. First conversion of a non-synthetic,
  non-corpus suite, so expect codegen bugs the corpus never exercised: real conftest layout, real
  plugin config, a vendored monorepo tree. Get it green under `velox --serial`, comparing against a
  `verify --record` baseline taken before `convert --write`.

- [ ] **7. Concurrency triage.** Raise concurrency, use the audit's hazard census as the triage
  index, hand-apply `@velox.solo`/`@velox.isolated` where tests fail. Automate only a pattern that
  repeats often enough to pay for a skill.

- [ ] **8. Write-up.** Both suites, audit findings, verify results, and httpx2's before/after
  concurrency. This is what the phase is for.

## Not blocking Phase 4

- [ ] **A suite with real conftest overrides.** Neither marshmallow nor httpx2 has one, so the
  Phase 3 specialization machinery — the most novel part of the tool, with no prior art behind it —
  has never run against a suite it was not written for. Find one and audit it before trusting the
  budget refusals.

- [ ] **Phase 6 — skills and prefactors.** The AI tiers, still unscoped. Scope it from what tasks 5
  and 7 actually needed rather than from the three-tier sketch below.

## Decisions summary

* velox itself grows no override mechanism
* the migration tool is a separate distribution — the velox runtime stays minimal.

| Question | Decision |
|---|---|
| Pipeline | `extract → audit → convert → verify`, artifact-coupled through a `.velox-migrate/` work dir |
| §11 Q1: require a working pytest collection? | Yes, but only of the extractor — a single-file plugin run in the suite's own env. No static fixture-resolution fallback, ever. |
| §11 Q2: preserve conftest layout or consolidate? | Preserve: one fixture module per directory that had a `conftest.py`. Consolidation is a post-migration cleanup skill. |
| §11 Q3: specialization budget | Per-override fan-out budget; over budget → loud refusal + pointer to the unwind-override prefactor skill |
| §11 Q5: propose DI seams? | Report the opportunity (audit) and assist the refactor (skill); `convert` never does it |
| §11 Q6: is `--concurrency 1` green a tool-enforced gate? | A subcommand (`verify`), strongly recommended in the workflow, not a hard gate — it requires both runners runnable in one env, which is not always true |
| §11 Q7: coverage verification | Documented recipe, not a v1 tool feature |
| Codegen platform | LibCST, alone, for audit, rewrite, and move |

# Planned scope

## The pipeline

Four subcommands. What each one does and how it is driven is in
[velox-migrate/README.md](../velox-migrate/README.md).

**`extract`** → `ground-truth.json`. Runs in the suite's environment; the only stage that needs pytest.
**`audit`** → `migration-report.md` + `findings.json` + terminal summary. Classifies every construct against the support matrix.
**`convert`** — the deterministic codegen. Dry-run is the default, `--write` is explicit.
**`verify`** — runs pytest on the pre-migration tree and `velox --serial` on the converted tree.

## Where AI fits: prefactor and postfactor, never convert

The organizing principle: **every rewrite that can be expressed as pytest→pytest runs before
conversion, while the suite is still green under pytest** — because there, pytest itself is the
verification oracle for each change. `convert` is reserved for the one rewrite that cannot exist in
pytest-land: the wiring swap. After conversion there is no oracle until `verify`, which is exactly
why `convert` must be deterministic, mechanical, and boring.

That gives three tiers, none of them built:

1. **Prefactor codemods** (deterministic, in `prefactor/`, run under pytest): `tmpdir` →
   `tmp_path`, legacy `pytest.raises(E, fn, args)` call form → context manager, unconditional
   `request.addfinalizer` → yield form, literal `request.getfixturevalue("x")` → a parameter,
   `usefixtures` on one test in a shared module → fixture moved so the mark can become
   module-level. Each shrinks `convert`'s surface, and each is verified by the suite staying green.

2. **Prefactor skills** (AI-assisted, in `skills/`, driven by `findings.json`): the judgment
   refactors. The skill proposes and applies a refactor *in pytest terms*.

3. **Postfactor skills**: triage after `verify` at concurrency — choosing `@velox.solo` vs
   `@velox.isolated` vs a seam per hazard site, and the optional consolidate-fixtures cleanup for
   teams that want the idiomatic layout after the reviewable diff has landed.

## Override chains without an override mechanism (§4.2)

With no override feature in velox, a conftest override has two translations:

- **Within budget**: `specialize.py` generates the specialized chain — the overriding fixture plus a
  copy of every fixture strictly between it and each test that resolves through it, with new names.
- **Over budget**: refuse loudly. The audit reports each override's fan-out, pointing at the
  unwind-override skill.

The budget's unit is *generated fixtures per override scope*, default ≈5.

# References

 * [migration-findings.md](migration-findings.md) — the corpus, the two real runs, and what they
   measured.
 * [migration-problem-statement.md](migration-problem-statement.md) — the constraints, cited above
   by §. Historical, but still the only statement of the full mapping surface.
