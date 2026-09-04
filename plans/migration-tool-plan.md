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

### Coexistence workspace

Blocks every task below that touches a real suite. `convert --write` rewrites the tree it is given
in place — the pytest suite is gone the moment it runs — so both real runs so far have needed a
suite copied out by hand first (marshmallow, see migration-findings.md), and `verify` takes
`--before`/`--after` as two tree arguments the user is responsible for keeping in sync. That was
tolerable for one conversion each; it is not tolerable for iterating on a codegen rule, which needs
to reconvert the same suite repeatedly while comparing against an untouched pytest baseline. Every
task from here on assumes it exists.

- [ ] **5. Decide the workspace's shape.** Candidates: `convert` grows a non-destructive `--out
  DIR` mode that mirrors the suite into a fresh tree instead of rewriting in place, replacing the
  ad hoc `cp -r` step; or velox-migrate grows a `workspace` subcommand that materializes
  `.velox-migrate/pytest/` (pristine) and `.velox-migrate/velox/` (regenerated from the pristine
  copy on every `convert` run) and points `extract`/`audit`/`convert`/`verify` at them by default.
  Decide against the friction actually hit so far rather than in the abstract — `verify` already
  takes two tree arguments, so the workspace may be mostly a matter of managing those two paths
  rather than new machinery. *Exit:* a decision recorded here, no code.

- [ ] **6. Build it, and adopt it.** Whichever shape task 5 picked. `verify` should default its
  `--before`/`--after` from the workspace instead of requiring both on every call; `just migrate`
  gets a recipe if one doesn't already fit. *Exit:* the marshmallow and `classes_showcase` corpus
  runs go through the new workflow instead of a manual copy, and the README's "copy the suite out
  of `oss/` first" instruction is replaced by it.

### Prefactor tier: unwinding autouse global-state fixtures

httpx2 is 88% serial on one autouse `clean_environ`; migration-findings.md already has flask
(`_standard_os_environ`, 77 monkeypatch sites) and rich (`reset_color_envvars`) at the same shape.
Three real suites hitting the identical pattern is what makes this a tier to build rather than a
one-off unwind, per the three-tier sketch below (prefactor codemod → prefactor skill →
postfactor). None of `prefactor/` exists yet.

- [ ] **7. Name the shape and the fix menu from the three real cases.** Lay httpx2's, flask's, and
  rich's sites side by side and decide, per site, which answer it takes — `[tool.velox] env` for
  what every test sets identically, a DI seam for what varies, `@velox.solo` for the residue that
  is neither. *Exit:* a table (in migration-findings.md, or a new prefactor-findings file it links)
  mapping every site across the three suites to one of the three answers, so task 8 has a spec
  instead of a hypothesis.

- [ ] **8. Build the deterministic slice as a prefactor codemod.** Whatever fraction of task 7's
  table is mechanical — e.g. `monkeypatch.setenv("X", "literal")` inside an autouse fixture
  becoming a suite-wide `[tool.velox] env` entry — as a pytest→pytest rewrite in `prefactor/`,
  verified by the suite staying green under pytest before conversion. *Exit:* run against rich (the
  volume check, smallest of the three) and remeasure its serial share.

- [ ] **9. Build the skill for the judgment slice.** Whatever task 7 decided needs a DI seam or
  per-site judgment, as the first prefactor skill, driven by `findings.json`. *Exit:* httpx2's
  `clean_environ` unwound end to end; its projected serial share, re-measured after tasks 8 and 9,
  is small enough that the before/after concurrency number is worth publishing — this is what was
  task 5's exit before the unwind grew into its own tier.

  httpx2's other prefactor is settled and needs no machinery: a suite-level `anyio_backend`
  returning `"asyncio"`, written by hand, pins the backend matrix and drops the `[trio]` half.

### Coverage comparison in `verify`

§11 Q7 punted this to a documented recipe. Formalizing it now: velox's isolated-subprocess coverage
merging (`velox/_run/coverage.py`) already gives the velox side of the primitive both runners need,
and it's the confidence signal `verify`'s outcome diff alone doesn't give — two suites can agree on
every outcome while covering different lines.

- [ ] **10. Decide the comparison's shape.** Line numbers do not survive the LibCST rewrite 1:1
  (lifted class fixtures, moved fixture modules, rewritten bodies), so "same lines covered" has to
  be coordinate-independent — e.g. per-source-file coverage percentage, or a mapping back through
  `convert/edits.py`'s own edit log rather than a second position-tracking mechanism built for this.
  *Exit:* a design note here, no code.

- [ ] **11. Wire coverage into both runner invocations.** `run_pytest`/`run_velox` in
  `verify/runners.py` gain a coverage-enabled mode, each writing its own data file into the
  workspace. *Exit:* two coverage data files land in `.velox-migrate/` after `verify --coverage`.

- [ ] **12. Diff and report.** Compare the two files per task 10's design, add a coverage section to
  `verify-report.md`/`verify.json`, decide whether a divergence fails `verify`'s exit status or is
  informational only. *Exit:* run against `classes_showcase` (marshmallow's corpus form, the
  suite with the cleanest baseline) and confirm the numbers agree modulo the five known outcome
  divergences.

### Resuming Phase 4 proper

- [ ] **13. httpx2: convert + verify.** Re-run the audit first — the recorded counts predate
  `@velox.filterwarnings` and `[tool.velox] filterwarnings`. First conversion of a non-synthetic,
  non-corpus suite, so expect codegen bugs the corpus never exercised: real conftest layout, real
  plugin config, a vendored monorepo tree. Get it green under `velox --serial`, comparing against a
  `verify --record` baseline and, per task 12, a coverage comparison.

- [ ] **14. Concurrency triage.** Raise concurrency, use the audit's hazard census as the triage
  index, hand-apply `@velox.solo`/`@velox.isolated` where tests fail. Automate only a pattern that
  repeats often enough to pay for a skill.

- [ ] **15. Write-up.** Both suites, audit findings, verify results, httpx2's before/after
  concurrency, and the coverage comparison. This is what the phase is for.

## Not blocking Phase 4

- [ ] **A suite with real conftest overrides.** Neither marshmallow nor httpx2 has one, so the
  Phase 3 specialization machinery — the most novel part of the tool, with no prior art behind it —
  has never run against a suite it was not written for. Find one and audit it before trusting the
  budget refusals.

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
| §11 Q7: coverage verification | Formalized as a `verify` feature (tasks 10–12), superseding the earlier documented-recipe answer |
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

That gives three tiers. The first two are getting a concrete case rather than a speculative build —
tasks 7–9 above scope them from httpx2/flask/rich's shared autouse-global-state shape:

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
