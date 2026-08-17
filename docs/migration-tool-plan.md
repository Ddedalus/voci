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

## 2. Package shape

```
velox-migrate/                    # workspace member; dist "velox-migrate", import velox_migrate
  velox_migrate/
    extractor.py                  # single-file pytest plugin, stdlib+pytest only, copyable
    schema.py                     # dump schema: versioning, loader, refusal on mismatch
    model.py                      # fixture graph + item model built from the dump
    matrix.py                     # the support matrix: every pytest construct → classification
    audit/                        # LibCST static scanners: §6 hazards, request.* uses, §5 traps
    rules/                        # VX001… LibCST codemod rules (the §5 mechanical layer)
    prefactor/                    # pytest→pytest codemod rules (run while the suite is green)
    layout.py                     # §4.5 planner: fixture-module placement, collisions, imports
    specialize.py                 # §4.2 chain generation + fan-out budget
    report/                       # terminal summary, migration-report.md, findings.json
    cli.py                        # extract | audit | convert | verify
  skills/                         # AI prefactor/postfactor skills (consume findings.json)
  scripts/                        # artifact regeneration (corpus dumps)
  corpus/                         # golden suites: before/, expected/, run-twice idempotency
    dumps/                        # their ground-truth dumps, one per supported pytest
  tests/
```

**Why a separate distribution:** the runtime must not carry LibCST, and the tool's lifecycle is
different — migration tools finish and get archived (bump-pydantic's arc); velox doesn't.

**Why the same repo:** the codegen targets velox's public API surface exactly, and drift between
them is the tool's biggest correctness risk. In one repo, the corpus tests can run their
*generated output under velox itself* in the same CI, so an API change that breaks generated code
breaks a test the same day. A separate repo would rediscover that drift at release boundaries.

**The extractor is deliberately a single file with no dependencies beyond pytest.** That is the
whole answer to "the suite only collects inside a container": copy one file in, run
`pytest -p extractor --collect-only`, copy one JSON file out. `velox-migrate extract` is a
convenience wrapper for the common case where the dev environment can collect; the file is the
portable unit. It pins `pytest>=8.4,<10` with three or four `hasattr` shims (the ground-truth
research enumerates them), and the dump carries `extractor_version` + `pytest_version` so the
codegen refuses mismatches loudly.

## 3. The extraction decision, and why there is no static fallback

The ground-truth research validated the extractor end to end: at collection time, without running
a test, pytest hands over per-test resolution with overrides already decided
(`name2fixturedefs`, chain ordered furthest→closest, winner last, the overridden super at `[-2]`
— exactly §4.2's input), the autouse placement map keyed by visibility node (exactly §4.3's
`velox.use` placement), per-case parametrize data with **pytest's generated id string captured
verbatim** (closing §9 by copy instead of by reimplementation), mark provenance via public
`iter_markers_with_node`, and resolved config plus the installed-plugin census for §7's
pre-flight.

The alternative — statically reimplementing conftest scoping, plugin fixture registration, and
`pytest_generate_tests` — is §4.1's own warning: rebuilding the machinery velox exists to delete,
with a permanent fidelity gap. A half-faithful static resolver is precisely the "test passes for
a new reason" failure mode ranked worst in §1. So the position is strict: **no dump, no
migration.** A suite that cannot collect under pytest cannot be migrated trustworthily by any
method; the tool says so and stops. Static analysis is still used heavily — but only for what
collection genuinely cannot see (test bodies: §5 traps, §6 hazards, `request.*` uses), never for
name resolution.

One consequence to document rather than fight: the dump is ground truth *for the environment it
ran in*. Platform-conditional fixtures need one extraction per relevant environment; the dump
records platform and plugin versions so `audit` can warn when it matters.

## 4. The pipeline

Four subcommands, coupled through files in `.velox-migrate/` so every stage is inspectable,
resumable, and re-runnable — partial migration (§10) falls out of this rather than being a mode.

**`extract`** → `ground-truth.json`. Runs in the suite's environment; the only stage that needs
pytest.

**`audit`** → `migration-report.md` + `findings.json` + terminal summary. Joins the dump with the
static scan and classifies every construct against the support matrix:

- *mechanical* — converted silently (the §5 table's clean rows);
- *mechanical-with-marker* — converted, but semantics shifted enough to warrant a
  `VELOX-TODO[category]` marker (capsys double-`readouterr`, caplog `set_level`, id-sensitive CI
  configs);
- *refused* — convertible only by a human decision: over-budget override chains,
  `pytest.skip()` in a body (§5's most dangerous rename), computed `getfixturevalue`;
- *unsupported* — the §3 "never" list and §7's no-recipe plugins, each with the manual path named;
- *hazard* — §6 census entries with counts and file:line lists, plus the headline
  **percent-of-suite-serialized estimate** that makes the adoption decision rational.

`audit` is read-only and valuable standalone — it is the "should we adopt velox at all?" scanner,
and it must work well *before* anyone commits to converting.

**`convert`** — the deterministic codegen. Dry-run is the default and prints the layout plan
(where each fixture module goes, every collision rename, every specialization chain with its
fan-out) plus a unified diff; `--write` is explicit. Byte-faithful edits to existing files;
freshly synthesized fixture modules are formatted once with the user's formatter and never
touched again. Every non-mechanical site gets `# VELOX-TODO[category]: reason`, with an
existence check making re-runs marker-idempotent.

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

2. **Prefactor skills** (AI-assisted, in `skills/`, driven by `findings.json`): the judgment
   refactors — unwinding an over-budget conftest override into an explicit seam or a
   parametrized base fixture; turning a `monkeypatch`/patched-global into an injected dependency;
   untangling an autouse fixture doing several jobs. The skill proposes and applies a refactor
   *in pytest terms*, the user runs pytest, green means proceed. This is where §11 Q5's DI-seam
   question lands: the idiomatic result needs seams, the reviewable diff argues against doing it
   in the conversion pass — so it happens in a different pass, with its own verification.

3. **Postfactor skills**: triage after `verify` at concurrency — choosing `@velox.solo` vs
   `@velox.isolated` vs a seam per §6 site, with the report supplying the context; and the
   optional consolidate-fixtures cleanup for teams that want the idiomatic layout after the
   reviewable diff has landed.

AI never touches the wiring translation because the failure ranking demands it: a model that
"helpfully" adjusts a fixture body during conversion is the silent-meaning-change failure, and
determinism plus idempotency are what make `convert` re-runnable on a moving branch.

## 6. Override chains without an override mechanism (§4.2)

With no override feature in velox, a conftest override has exactly two honest translations, and
the tool offers both:

- **Within budget**: `specialize.py` generates the specialized chain — the overriding fixture
  plus a copy of every fixture strictly between it and each test that resolves through it, named
  by scope (`settings_integration`, `engine_integration`, …), placed in the overriding
  directory's fixture module. The dump's per-test chains make this mechanical, including the
  "override requests its super" pattern (`[-2]`).
- **Over budget**: refuse loudly. The audit reports each override's fan-out (how many downstream
  fixtures × how many override scopes) so the expensive ones are visible before conversion; the
  refusal points at the unwind-override skill, and the prefactored suite converts cleanly on the
  next run.

The budget's unit is *generated fixtures per override scope*, default deliberately small (≈5,
configurable). The rationale: a 2-fixture chain duplicated for one subtree reads fine in review;
a 15-fixture chain duplicated for three subtrees is the 40,000-line diff §4.2 warns about, and
that suite is better served by a seam it will want anyway. Erring toward refusal is consistent
with the failure ranking, and the escape hatch is a skill run, not hand edits.

## 7. Layout (§4.5)

Preserve the conftest geography: each directory that had a `conftest.py` gets a `fixtures.py`
(the non-fixture conftest content — helpers, constants — moves alongside; hooks are refused with
their own marker category). Rationale: the diff stays reviewable because every fixture moves the
shortest possible distance, the override/specialization structure stays legible in the tree, and
name collisions stay rare because the directory keyed them apart in pytest too. Where two
`fixtures.py` still collide at an import site, the import aliases by path
(`from tests.integration.fixtures import client as integration_client`). All imports are emitted
rootdir-relative absolute — never relative — because test modules load under synthetic
`velox_tests.*` names. `velox.use` placement comes straight from the dump's autouse map: a
visibility node of `integration` becomes a declaration in `integration/__init__.py`, created if
absent, importing from the sibling `fixtures.py`.

## 8. Codegen platform and discipline

LibCST ≥ 1.9, alone (evidence and alternatives in the research doc — ast-grep is detection-grade
only, tokenize-hybrid is the wrong shape for decorator/signature/layout surgery, everything else
is dead). The disciplines adopted from prior art:

- **bump-pydantic's registry shape**: numbered rules (`VX001…`), individually disableable, each
  mapping one row of the support matrix, so report categories, marker categories, and rule ids
  reconcile by grep.
- **pyupgrade's fixpoint idempotency**: every rule matches only pytest source forms, which the
  rewrite eliminates — idempotent by construction — plus explicit marker-existence checks (the
  one place bump-pydantic's history shows that construction fails).
- **django-codemod's `CodemodTest` pattern**: verbatim before/after source assertions per rule,
  which double as format-preservation pins.
- **The corpus as the real test bed**: golden suites converted end-to-end, run twice
  (byte-identical second run asserted), and their output executed under velox in CI — the
  drift alarm that justifies the monorepo.
- `QualifiedNameProvider` everywhere a `pytest` spelling is matched (aliased imports),
  `AddImportsVisitor` for imports, `SkipFile` for per-file refusal.

## 9. Build order

Ordered by risk retired per unit of work; each phase has a checkable exit.

- [x] **Phase 0 — extractor + schema.** Harden the validated plugin (version shims,
  `_ini_aliases`, platform stamp, `--extractor-out` option), freeze dump schema v1, write the
  loader/model. *Exit: dumps from pytest 8.4 and 9.x load into one model; corpus suite dumps
  checked in.* Three things the build settled that this document had guessed at: the corpus
  lives at `velox-migrate/corpus/`, not under `tests/`, because a `collect_ignore` in a parent
  conftest also fires when pytest is pointed straight at the corpus; the dump deduplicates
  fixture definitions into a `fixture_defs` table that chains reference by key, since inlining a
  chain per test grows a dump with fixtures × tests; and visibility needed a shim the research
  had not predicted — pytest 9.1 gives the rootdir conftest its own node (`"."`) while 8.4
  spells it `""`, the same string it uses for globally-registered plugin fixtures, so an 8.4
  conftest fixture is re-keyed to the directory it was written in.
- [x] **Phase 1 — audit.** Support matrix as data, static hazard scanners, report + findings.json.
  Standalone value: run it against two real OSS async suites and publish what it says.
  *Exit: audit of a real suite whose numbers survive manual spot-checks* — met against velox's own
  891-test suite, where the monkeypatch, `pytest.skip()`, warning-filter and event-loop counts match
  a grep of the tree exactly and the two discrepancies were the scan being right and the grep wrong.
  The write-up of two OSS suites is still to do: it needs an environment per suite, since the
  vendored `fastapi/` does not collect against the starlette in this workspace, and a suite that
  cannot collect has no dump. Five things the build settled:
  - **A finding needs a blast radius, not just a site.** The percent-of-suite-serialized number is
    only meaningful if a hazard written in an autouse fixture is charged to every test that inherits
    it, so `audit/reach.py` maps a `file:function` back onto node ids through the dump's own fixture
    closures, walking enclosing scopes so a nested helper is charged to its test rather than to its
    file. On the corpus suite this turns one `monkeypatch.setenv` in a root autouse fixture into
    100% of the suite running alone, which is the whole point of quoting the number.
  - **Every construct needs exactly one witness.** The dump and the scan can both see per-case
    marks, string skipif conditions and `setup_method`; counting a construct twice inflates the
    census. The rule that fell out: the dump is the census of the mechanical surface (fixtures,
    cases, marks, configuration) and the scan is the trouble finder (anything inside a body), with
    the scan owning the shapes whose *arguments* have to be read.
  - **Constructs come at two scopes, and conflating them destroys the verdict.** A conftest hook or
    an ini setting blocks no individual test, but charging it to every test in its file reported a
    whole suite as unconvertible over one `conftest.py`. `Construct.suite_level` marks those rows
    and their findings name no tests.
  - **Configuration has to be read from the suite's own ini file, not from pytest's resolved
    values,** which cannot distinguish a plugin's default from a line someone wrote — and reported
    under the spelling the suite used, since pytest renames settings and keeps aliases, so the same
    suite otherwise audits differently under 8.4 and 9.1. Two tables the plan had not foreseen make
    the rest decidable: pytest's own builtin fixtures and its own ini keys, since "is this a
    plugin's?" is only answerable by knowing what pytest itself provides.
  - **The dump answers two constructs nobody expected it to.** pytest wraps a `setup_method` or a
    `TestCase` in synthetic `_xunit_*`/`_unittest_*` fixtures, which name those class lifecycles
    without reading a line of source; and because the dump carries the ids `pytest_generate_tests`
    generated, a suite that parametrizes through that hook converts to an explicit
    `@velox.parametrize` rather than being refused.
- **Phase 2 — mechanical convert.** The §5 table, fixture/mark/parametrize/ids/config
  translation, layout planner, import emission — overrides refused wholesale at this phase.
  *Exit: the §1 bar on no-override corpus suites — collects and passes under `velox --serial`
  with zero hand edits, twice-run byte-identical.*
- **Phase 3 — the hard §4 machinery.** Specialization within budget, autouse placement, request
  elimination, `mock.patch` handling (decorator reorder + `@velox.solo` at context-manager
  sites). *Exit: corpus suites with overrides and autouse pass; over-budget cases refuse with
  correct fan-out numbers.*
- **Phase 4 — verify + prefactor codemods + skills.** The outcome-comparison gate, the
  pytest→pytest rules, then the skills in the order their findings appear in real audits.
  *Exit: one real OSS suite migrated end-to-end through the full ladder, written up.*

## 10. Risks not already covered

- **pytest internals churn** is confined to `extractor.py` by construction; the codegen's only
  pytest coupling is the dump schema, which is versioned. Accepted.
- **LibCST governance** (Meta-driven): pinnable for the tool's whole life, since the tool is not
  a long-lived service. Accepted.
- **The novel passes have no prior art** (§4.1/§4.2 — nobody has shipped fixture-graph
  specialization); effort estimates for Phase 3 are uncalibrated. Mitigated by phase order:
  Phases 0–2 ship value even if Phase 3's budget ends up tighter than hoped.
- **Sequencing with the ROADMAP's open "injection ergonomics" decision**: this plan assumes the
  answer stays "no overrides". If that reverses, `specialize.py` shrinks dramatically and the
  budget logic becomes the override-emission logic — the pipeline, extractor, audit, and layout
  work are unaffected either way, which is another argument for building them first.
