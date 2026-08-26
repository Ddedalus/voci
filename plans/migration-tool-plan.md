# pytest → velox migration tool: implementation plan

Internal working document, companion to
[migration-problem-statement.md](migration-problem-statement.md) (cited below by §). Where the
problem statement deliberately proposed no design, this document proposes one: the shape of the
companion package, the pipeline, the codegen platform, and the build order. Evidence behind the
platform and extraction decisions lives in
[research/migration/codegen-libraries.md](../research/migration/codegen-libraries.md) and
[research/migration/pytest-ground-truth.md](../research/migration/pytest-ground-truth.md).

Two constraints fixed by prior decisions, taken as given here: velox itself grows no override
mechanism (the ROADMAP "injection ergonomics" item stays unbuilt), and the migration tool is a
separate distribution — the velox runtime stays minimal.

---

## 1. Decisions at a glance

| Question | Decision |
|---|---|
| Where does the tool live? | `velox-migrate`, a second distribution in this repo (uv workspace member); velox gains no dependency on it |
| §11 Q1: require a working pytest collection? | **Yes — but only of the extractor**, a single-file pytest plugin run in the suite's own env; the codegen consumes its JSON dump and never imports pytest. No static fixture-resolution fallback, ever. |
| Codegen platform | LibCST ≥ 1.9, alone, for audit, rewrite, and move (see research doc; confidence high) |
| Pipeline | `extract → audit → convert → verify`, artifact-coupled through a `.velox-migrate/` work dir |
| Where AI fits | Prefactor and postfactor skills operating on audit artifacts, while a green test suite exists to verify against. **Never inside `convert`.** |
| §11 Q2: preserve conftest layout or consolidate? | Preserve: one fixture module per directory that had a `conftest.py`. Consolidation is a post-migration cleanup skill. |
| §11 Q3: specialization budget | Per-override fan-out budget; over budget → loud refusal + pointer to the unwind-override prefactor skill |
| §11 Q5: propose DI seams? | Report the opportunity (audit) and assist the refactor (skill); `convert` never does it |
| §11 Q6: is `--concurrency 1` green a tool-enforced gate? | A subcommand (`verify`), strongly recommended in the workflow, not a hard gate — it requires both runners runnable in one env, which is not always true |
| §11 Q7: coverage verification | Documented recipe, not a v1 tool feature |


**Why the same repo:** the codegen targets velox's public API surface exactly, and drift between
them is the tool's biggest correctness risk. In one repo, the corpus tests can run their
*generated output under velox itself* in the same CI, so an API change that breaks generated code
breaks a test the same day. A separate repo would rediscover that drift at release boundaries.

**The extractor is deliberately a single file with no dependencies beyond pytest.** That is the whole answer to "the suite only collects inside a container": copy one file in, run
`pytest -p extractor --collect-only`, copy one JSON file out. `velox-migrate extract` is a
convenience wrapper.

The dump is ground truth *for the environment it ran in*. Platform-conditional fixtures need one extraction per relevant environment; the dump records platform and plugin versions so `audit` can warn when it matters.

## 4. The pipeline

Four subcommands, coupled through files in `.velox-migrate/` so every stage is inspectable,
resumable, and re-runnable.

**`extract`** → `ground-truth.json`. Runs in the suite's environment; the only stage that needs pytest.

**`audit`** → `migration-report.md` + `findings.json` + terminal summary. Joins the dump with the static scan and classifies every construct against the support matrix:

- *mechanical* — converted silently (the §5 table's clean rows);
- *mechanical-with-marker* — converted, but semantics shifted enough to warrant a
  `VELOX-TODO[VXnnn]` marker (capsys double-`readouterr`, caplog `set_level`, id-sensitive CI
  configs);
- *refused* — convertible only by a human decision: computed `getfixturevalue`;
- *unsupported* — the §3 "never" list and §7's no-recipe plugins, each with the manual path named;
- *hazard* — §6 census entries with counts and file:line lists, plus the headline
  **percent-of-suite-serialized estimate** that makes the adoption decision rational.

`audit` is read-only and valuable standalone — it is the "should we adopt velox at all?" scanner,
and it must work well *before* anyone commits to converting.

**`convert`** — the deterministic codegen. Dry-run is the default and prints the layout plan
(where each fixture module goes, every collision rename, every specialization chain with its
fan-out) plus a unified diff; `--write` is explicit. Byte-faithful edits to existing files;
freshly synthesized fixture modules are formatted once with the user's formatter. Every non-mechanical site gets `# VELOX-TODO[VXnnn]: reason`.

**`verify`** — runs pytest on the pre-migration tree and `velox --serial` on the converted tree,
and compares outcomes test-for-test through the id map (trivial, since ids were emitted
verbatim). Divergence lists are the review queue. Then the user raises concurrency and the §6
census becomes the triage index. Recommended in the workflow, not enforced: requiring both
runners in one environment is a real constraint, and refusing to convert without it would gate
the tool on the hardest environments.

## 5. Where AI fits: prefactor and postfactor, never convert

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

The budget's unit is *generated fixtures per override scope*, default≈5,
configurable.

## 7. Layout (§4.5)

Preserve the conftest geography: each directory that had a `conftest.py` gets a `fixtures.py`
(the non-fixture conftest content — helpers, constants — moves alongside; hooks are refused with
their own marker category).

Where two `fixtures.py` still collide at an import site, the import aliases by path (`from tests.integration.fixtures import client as integration_client`). 

## 8. Codegen platform and discipline

LibCST ≥ 1.9. The disciplines adopted from prior art:

- **bump-pydantic's registry shape**: numbered rules (`VX001…`)
- **pyupgrade's fixpoint idempotency**: every rule matches only pytest source forms, which the rewrite eliminates.
- **django-codemod's `CodemodTest` pattern**: verbatim before/after source assertions per rule, which double as format-preservation pins.

## 9. Build order

Ordered by risk retired per unit of work; each phase has a checkable exit.

- [x] **Phase 0 — extractor + schema.** Single-file pytest plugin dumps a versioned JSON ground-truth
  model of a suite's fixtures, cases, marks, and config; loader/model and corpus dumps checked in.
- [x] **Phase 1 — audit.** Static hazard scanners plus the dump produce a support-matrix classification,
  blast-radius report, and findings.json, validated against velox's own suite and a corpus suite.
- [x] **Phase 2 — mechanical convert.** The no-override §5 table (fixtures/marks/parametrize/ids/config,
  layout, imports) converts a corpus suite with nothing refused, passing `velox --serial` byte-identical.
- [x] **Phase 3 — the hard §4 machinery.** Declaration placement (autouse/`usefixtures`), fixture-graph
  specialization within budget, `request` elimination, and `mock.patch` handling all convert across seven
  corpus suites with nothing refused (over-budget chains refuse with correct fan-out); `plan.DEFERRED` is
  empty.
- **Phase 4 — verify + prefactor codemods + skills.** The outcome-comparison gate, the
  pytest→pytest rules, then the skills in the order their findings appear in real audits.
  *Exit: one real OSS suite migrated end-to-end through the full ladder, written up.* Target
  suites: marshmallow (smoke — expected zero refusals) and httpx2, pydantic's fork of httpx
  (exit suite). Selection methodology and upstream-httpx numbers are in
  [oss-refactors-plan.md](oss-refactors-plan.md), where httpx2's row is now measured rather than
  inherited from httpx — see [httpx2-audit.md](httpx2-audit.md). Sequenced as:

  - [x] **0. Scope httpx2** (spike, no code). Viable, and four times the size the inherited httpx
    numbers said. It does still run on trio, in a way neither question anticipated: anyio's own
    `anyio_backend` fixture is parametrized over both backends, so 294 of 1991 cases are the trio
    half of an async matrix the suite never writes. The fixture graph is simpler than flask's, with
    no overrides. The spike was not free of code after all — it found four audit defects, all of
    them a name read without asking who wrote it, and the numbers below are the ones after the fix.
    See [httpx2-audit.md](httpx2-audit.md).
  - [x] **1. `verify` subcommand.** Runs pytest on the pre-migration tree and `velox --serial` on
    the converted tree, diffs outcomes through the id map, writes the divergence list. pytest's
    half reports through `outcomes.py`, a second copyable single-file plugin, since a terminal
    summary is not a per-test record; velox's is read off `-v`'s own lines. `--record` takes the
    pytest half before `convert --write` overwrites the tree it would have run in.
  - [x] **2. marshmallow: convert + verify + corpus-ify.** Done: 1183 of 1188 tests pass under
    `velox --serial` and the same 1183 at full concurrency, with nothing refused. The bug hunt
    found more than expected — see [marshmallow-migration.md](marshmallow-migration.md) for what
    it turned up and what the five failures are. Corpus-ified as `classes_showcase` rather than as
    a checked-in marshmallow dump: the dump is 2 MB per pytest version against 504 KB for the
    whole existing corpus, and the machinery marshmallow exercised is what a showcase suite pins.
  - [x] **3. httpx2: audit findings write-up.** Done with step 0, since fixing what the audit got
    wrong was what produced the numbers worth writing up. 342 blocked of 1991, 88.0% serial on one
    autouse `clean_environ`, no override chains.
  - **4. First prefactor codemod(s).** `prefactor/` still does not exist, and step 3's findings do
    not clearly call for it: httpx2's one prefactor is a suite-level `anyio_backend` fixture
    returning `"asyncio"`, which is a fixture to write rather than a rule to run. Write it by hand
    for step 5 and let the tier stay unbuilt unless a second suite wants the same rule. Don't build
    the general framework speculatively.
  - **5. httpx2: convert + verify.** First conversion of a non-synthetic, non-corpus suite; expect
    codegen bugs the corpus suites didn't exercise (real conftest layout, real plugin config). Get
    it green under `velox --serial`.
  - **6. Concurrency triage.** Raise concurrency, use the audit's hazard census as the triage
    index, hand-apply `@velox.solo`/`@velox.isolated` at whatever sites fail. Only build a
    postfactor skill if the same pattern repeats often enough to be worth automating — otherwise
    stays manual and the skill tier stays out of scope for Phase 4.
  - **7. Write-up.** The exit deliverable: both suites, audit findings, verify results, and
    httpx2's before/after concurrency.

  Steps 0–3 are done. 0 and 3 were meant to gate whether 4–6 are needed at all: 4 stays unbuilt,
  and 5–6 are where the size of the suite starts to bite.

## 10. Risks not already covered

- **pytest internals churn** Accepted.
- **LibCST governance** Accepted.
- **The novel passes have no prior art** (§4.1/§4.2 — nobody has shipped fixture-graph specialization); effort estimates for Phase 3 are uncalibrated.