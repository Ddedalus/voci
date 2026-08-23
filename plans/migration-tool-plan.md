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
- [x] **Phase 2 — mechanical convert.** The §5 table, fixture/mark/parametrize/ids/config
  translation, layout planner, import emission — overrides refused wholesale at this phase.
  *Exit: the §1 bar on no-override corpus suites — collects and passes under `velox --serial`
  with zero hand edits, twice-run byte-identical* — met against a third corpus suite written for
  it, whose 40 tests convert with nothing refused, pass under `velox --serial`, and keep every one
  of pytest's node ids. Six things the build settled:
  - **The marker carries the code, not the category.** This document had said
    `VELOX-TODO[category]`, but a category exists only for the rows that convert with a caveat, so
    a refusal had none to name — and the code is what a report section, a finding and a rule
    already reconcile against, which is the whole point of having one.
  - **Refusal is the load-bearing half, and it travels.** A refused test keeps its pytest
    signature, so velox reports it as a collection error naming that test: the partial migration
    §10 asks for announces itself instead of running a test that means something new. And a
    fixture nothing can translate refuses every fixture downstream of it and every test that
    reaches it, because a `Depends()` naming an object nobody built is worse than no rewrite.
  - **`convert` re-decides nothing; it reads the audit.** Every classification is already a
    matrix row with a site and a blast radius, so a phase boundary is a list of codes rather than
    a fork in the rewriter — Phase 3 takes a code out of `plan.DEFERRED` and adds its rule.
  - **A fixture's binding name is not its argname, and only the source knows it.** The dump
    records where a *factory* is, which a decorator can move to another module entirely, so the
    owning file comes from visibility and the name to import comes from the module-level `def` in
    it. A fixture written inside a class or a function binds nothing importable, and leaving it
    out of that table is what refuses it.
  - **Stacked `parametrize` marks have to be reversed, and that is what keeps §9's promise.**
    pytest applies decorators bottom-up, so its innermost axis varies slowest and is written first
    in a case id; velox reads its own list outermost-first. Reversing makes the two agree on both
    the id and the case order, and changes nothing else. Where an id still cannot be attributed to
    one axis, `VX114` says so — conservatively, since ids composed of plain strings agree anyway.
  - **Signature whitespace survives unless the order has to change.** Injected parameters gain
    defaults, and a parameter without one cannot follow them, so only a signature mixing fixtures
    with `parametrize` argnames is reordered — velox binds by keyword, so that order is free.
- [x] **Phase 3 — the hard §4 machinery.** Specialization within budget, autouse placement, request
  elimination, `mock.patch` handling (decorator reorder + `@velox.solo` at context-manager
  sites). *Exit: corpus suites with overrides and autouse pass; over-budget cases refuse with
  correct fan-out numbers.* Declaration placement — autouse fixtures and `usefixtures`, `VX008`
  through `VX010` — is built, against a fourth corpus suite whose tests convert with nothing
  refused, pass under `velox --serial` and keep every node id. Five things that build settled:
  - **Autouse and `usefixtures` are one construct in two spellings, and the dump says so.** Both
    ask for a fixture the test never names, and pytest keys both by the node they became visible
    at, so both are answered by placing one `velox.use(...)` on the module or the package that
    node stands for. The mark rule that takes `@pytest.mark.usefixtures` away writes nothing in
    its place; where the declaration goes is read from the dump, not from the mark.
  - **The ini file's `usefixtures` needs no case of its own.** pytest registers it at the session
    node, which the extractor already folds into the rootdir, and the fixture it names is autouse
    nowhere — so resolving a node's names against the tests that node covers, rather than against
    the table of autouse definitions, answers it and the plugin-registered fixture together.
  - **A declaration is code, so it obeys import order.** It goes after the imports, where a reader
    looks for what a module pulls in — except in a module declaring a fixture written in its own
    body, where a `velox.use` above the `def` would read a name nothing has bound yet.
  - **The package chain is part of the placement.** velox reads a package's declarations by
    walking up from the test file, so an `__init__.py` missing anywhere between a declaring
    directory and a test under it is a declaration that silently reaches nothing. The conversion
    writes the empty ones as well as the declaring ones.
  - **A declaration a refused test asked for is not written.** That test keeps the mark it was
    written with and is reported as refused, and declaring on its behalf would widen a fixture
    onto the module's other tests for nobody's benefit.

  What remains is sequenced in four chunks, each with its own corpus suite and exit bar:

  - **Chunk 1 — specialization I: the override itself** (`VX005` at fan-out 1, `VX006`). The
    overriding fixture becomes its own object in the overriding directory's `fixtures.py`, and
    every test under that node imports it instead of the base. The load-bearing detail is the
    super pattern: an override requesting its own name resolves to `chains[-2]`, which
    `Item._super_of` already answers, so the emitted `Depends()` names the base object; the
    layout's directory-aliasing already handles both landing in one importing module. The design
    call it forces is that `plan.refused()` gates per code, and converting only fan-out-1
    overrides needs a finding-level gate reading `detail["fan_out"]` — the seam that makes Chunk 2
    an increment rather than a rewrite. *Exit: a leaf-override suite converts with nothing
    refused, passes `velox --serial`, keeps every node id, and runs twice byte-identical, while
    `fixtures_showcase` still refuses `engine`'s chain.*
  - **Chunk 2 — specialization II: the chain copies** (`VX005` in general). `specialize.py`: for
    each override, copy every fixture strictly between it and each test resolving through it,
    named by scope (`settings_integration`, `engine_integration`), placed in the overriding
    directory's fixture module, with the whole subtree's imports rewired to the copies. The
    fan-out gate goes, leaving the budget refusal as the only boundary. *Exit:
    `fixtures_showcase`'s override half converts, an over-budget chain refuses with the fan-out
    the audit computed, and `--budget` moves that line end to end.* This is the piece §10 calls
    uncalibrated — no prior art for fixture-graph specialization — and if it overruns, the honest
    fallback is a low default budget and more suites refusing.
  - **Chunk 3 — what the body scanner found** (`VX011`, `VX013`, `VX217`, `VX218`).
    `request.getfixturevalue("literal")` becomes an ordinary `Depends()` parameter, since the
    dump's closure already resolved the name; an unconditional `request.addfinalizer(fn)` becomes
    `yield` teardown, with `VX014`'s conditional form still refused; `mock.patch` as a decorator
    keeps its patch and gains `@velox.solo`, with injected parameters emitted after the mock
    arguments the decorator fills positionally — an extension of the signature reordering Phase 2
    already does — and as a context manager it gains `@velox.solo` alone. `_propagate` and
    `wiring._request_is_only_param` both have to learn that a `request` used only for these
    shapes is eliminable.
  - **Chunk 4 — parametrize completion** (`VX007`, `VX024`). Indirect parametrize: the matrix row
    says "one generated fixture per value", and a generated `params=` fixture carrying the values
    is the closer match to what indirect means — settling that row is the chunk's first job.
    `pytest_generate_tests` cases become an explicit `@velox.parametrize` from the dump's frozen
    callspecs with pytest's own ids, refusing cases whose `repr`ed values do not round-trip
    through `literal_eval`, since a dump records `repr(obj)` and that is not always source.

  Chunks 1 and 2 are built as one pass, since the general case subsumes the fan-out gate and a
  finding-level seam bought nothing once the copies existed. The subject is a fifth corpus suite
  whose ten tests convert with nothing refused, pass under `velox --serial` and keep every node
  id, with `--budget 2` turning its three-fixture chain into a refusal quoting the fan-out the
  audit computed. Five things that build settled:
  - **A copy is a re-binding, not a rewrite.** The duplicated `def` is spliced into its new module
    in the pytest spelling it was written in, before a single rule runs, so the ordinary
    conversion translates it exactly as it translates the definitions already there. Nothing in
    the specialization pass reads a fixture body, and there is one code path for a fixture a
    person wrote and one the tool wrote.
  - **A `Depends()` is read where its `def` is, so placement is ordering.** A copy goes below
    everything it names and above everything that names it, which lands the chain between the
    override it specializes and the fixtures written beside that override which consume it.
  - **A fixture's edges resolve from where its object is written, not from whichever test reached
    it first.** Once overrides convert, asking any test how `engine` resolves `settings` can
    answer with the override — which wires the root object to a subtree's definition and leaves
    the two fixture modules importing each other. The chain carries every definition; the node
    doing the asking picks one.
  - **A fixture written at or below the override is wired to it, not copied for it.** Every test
    that can see such a fixture already resolves the override, so a copy would be a second object
    nothing names, and counting it would spend budget on a duplicate that never gets written.
  - **An autouse fixture anywhere in the chain has no honest translation.** A `velox.use(...)`
    names one object for a directory, so two definitions of an autouse name would both be
    declared over tests that had exactly one. `VX027` refuses the overriding definition and only
    that one: what the subtree loses is a conversion, and what the rest of the suite keeps is the
    definition it overrode, declared for the tests that converge on it.

  Chunk 3 is built against a sixth corpus suite, whose eleven tests convert with nothing refused,
  pass under `velox --serial`, keep every node id and run twice byte-identical. Six things that
  build settled:
  - **A `request` parameter goes away whole or not at all.** The rewrite's product is a signature
    without it, so it can only be written where every use of `request` in that body has an answer:
    a factory reading a literal `getfixturevalue` *and* `request.node` converts neither. That made
    the scan's silence load-bearing, and it was not silent enough — an attribute with no row of
    its own was reported by nothing, so `VX015` now covers every attribute of `request` rather
    than the five that were named.
  - **The rules decide none of this; the plan does, and reads source to do it.** Whether a name
    means one fixture is a question about the whole suite, and whether a factory has one place a
    `yield` can go is a question about a shape no dump carries — so the plan answers both, from
    the audit and from the `ast` it already parses for binding names, and hands each rule the
    sites it may write in. A rule that re-derived either would be a second opinion in the one
    stage that has no oracle behind it.
  - **A literal name is static only where the suite defines it once.** pytest resolves
    `getfixturevalue` against the test that is running, so a name defined in two directories
    reaches two objects and a parameter names one. `VX028` refuses those, and a body sitting
    somewhere no signature can grow — a helper, a fixture written in a class — with it.
  - **A finalizer is a teardown only where the body has one place to yield from.** A `return`
    inside a branch would become a `yield` the body then runs past, so what converts is a factory
    with no `yield` of its own and no `return` except as its last statement; everything else is
    `VX014`, whose row grew to say so. The calls are written after the `yield` in the reverse of
    the order they were registered in, which is the order pytest ran them in.
  - **A specialized copy carries the body's answers too.** A copy is the original's source under
    another name, so the name that body asked for is injected into the copy, with the import that
    reference needs, and the copy's finalizer becomes its own teardown. Keying what the plan
    decided by the name the file binds — rather than by the definition it was decided about — is
    what makes the two the same code path.
  - **The decorator form of `mock.patch` needed no mark at all.** velox reads a test's patching off
    the function object while collecting and schedules that test alone, so what the conversion owes
    it is a signature: the mock arrives first and positionally, and the injected parameters follow
    it — which is what Phase 2's reordering already writes, since an injected parameter is the one
    that has a default. Only a patch entered inside a body needs `@velox.solo`, and it goes on the
    tests the audit charged the site to, which are not always in the file the patch is written in.

  Chunk 4 is built against a seventh corpus suite, whose ten tests convert with nothing refused,
  pass under `velox --serial`, keep every node id and run twice byte-identical, and it closes
  Phase 3 — `plan.DEFERRED` is empty. Five things that build settled:
  - **The row said "one generated fixture per value", and what indirect means is one fixture with
    the values.** `params=` is the same multiplication the mark asked for, written where velox
    reads it, while a fixture per value would be objects nothing names. That leaves the mark with
    nothing to become, so `VX007` joins `VX009` as a wiring row spelled as a mark: the rule takes
    the decorator away and the wiring swap writes the cases onto the fixture.
  - **Both case lists are written from the dump rather than from the source that produced them.**
    A hook's cases have no source to copy, and a mark's values are written in a test module while
    the `params=` they become is read in whichever module holds the fixture — so what travels is
    the value pytest passed, spelled as a literal, and a `repr` that does not read back as one is
    a case this refuses. What the mark itself has to be is a decorator on the test's own `def`,
    since that is the only place the rewrite takes one away — a mark on a class or in a
    `pytestmark` would leave the fixture parametrized twice over.
  - **A `params=` fixture has one case list, so the whole suite has to agree on it.** pytest
    decides a fixture's cases per test, and what makes the move honest is that every test reaching
    that fixture asked for the same ones — including the tests that reach it without naming it,
    which would otherwise gain cases nobody wrote for them. `VX029` refuses the rest, sited on the
    test rather than on the fixture, since the fixture is usually right for every other test that
    reaches it.
  - **Where an id sits is what decides whether an axis can move at all.** velox composes a case id
    with its fixture-`params=` axes first and pytest composes it in the order the axes were
    registered, so an indirect axis converts only where pytest already put it first — which is the
    mark written innermost. The position falls out of the same id attribution `VX114` already
    does, so one reading of a test's axes answers both questions.
  - **A hook is not a construct that converts; its cases are.** `pytest_generate_tests` stays
    where it was written and is marked, because velox has no collection hook — and the axes it
    produced become decorators on the tests, outermost first so velox composes each id in the
    order pytest did. An axis the hook composed with one the test was written with is refused
    rather than slotted into a decorator stack whose order another rule owns.
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
