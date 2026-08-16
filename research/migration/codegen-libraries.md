# Codegen library landscape for the pytest→velox migration tool

Survey of Python source-rewriting libraries and prior-art migration tools, evaluated against the
requirements in [docs/migration-problem-statement.md](../../docs/migration-problem-statement.md)
§10: comment/format-preserving rewrites, idempotency, dry-run + report, in-source markers for
non-mechanical sites. Environment: tool runs on Python 3.13; target suites 3.10+. Facts verified
against live sources, August 2026.

## TL;DR recommendation

**Build the whole tool on LibCST (≥1.9.0), and nothing else, for all three passes.** Confidence:
high for the rewrite pass (~95%), high for detection (~85%), moderate-high for cross-file
move/rename (~80% — the cross-file logic is custom either way; LibCST just supplies the best
substrate).

- **Detection/audit pass**: LibCST with metadata providers (`QualifiedNameProvider`,
  `ScopeProvider`) for everything that reads test source; pytest's own collection (via a
  `--collect-only`-style plugin dump) as the authority for §4.1 name resolution where a working
  collection exists. Plain `ast` is faster for read-only hazard scans, but a second node
  vocabulary buys ~seconds on a one-shot tool and costs a duplicated matcher layer — not worth it.
- **Rewrite pass**: LibCST codemod framework (`CodemodCommand` per rule, bump-pydantic-style rule
  registry), `AddImportsVisitor`/`RemoveImportsVisitor` for idempotent import management, matcher
  decorators for structural targeting. No credible alternative exists in 2026 for
  format-preserving *structural* rewrites (decorators, parameter defaults, signatures, new files).
- **Cross-file fixture move/rename**: custom logic over a fixture-graph model (fed by pytest
  collection data), with LibCST `FullRepoManager` + `FullyQualifiedNameProvider` for cross-module
  name resolution and LibCST for the edits at both ends. Not rope, not ast-grep.
- **Rejected**: tokenize hybrid (pyupgrade-style) — right for local syntax swaps, wrong for
  signature/decorator/layout surgery; ast-grep — excellent detection grep, but its rewrite model
  (textual replacement of one matched node, no scope/import machinery) is below the fidelity bar;
  rope — IDE-session oriented, wrong platform for a batch pipeline; everything else is dead.

Speed is a non-issue: this is a run-a-few-times migration, not a per-commit linter. LibCST's Rust
parser plus `parallel_exec_transform_with_prettyprint`'s multiprocess fan-out handles
thousands-of-file suites in minutes, which is fine; the 100–300× speed edge of the tokenize
approach solves a problem this tool does not have.

---

## Part A — rewriting libraries

### LibCST — the platform (recommended)

- **Health (verified)**: actively maintained by Meta/Instagram. 1.9.0 released **2026-07-29**
  (adds Python **3.15** grammar); steady 1.8.x cadence through 2025 (1.8.6 2025-11-03, 1.8.3
  2025-08-29, 1.8.0 2025-05-27). Free-threaded CPython wheels; PyO3 kept current (0.26).
  [CHANGELOG](https://github.com/Instagram/LibCST/blob/main/CHANGELOG.md),
  [releases](https://github.com/Instagram/LibCST/releases).
- **Grammar**: parses Python 3.0→3.15, so target suites at 3.10+ and the 3.13 host are fully
  covered with headroom. The **Rust native parser is the default**; the pure-Python parser's entry
  points were removed in 1.8.3 (2025-08-29) — the old "LibCST is slow because its parser is
  Python" complaint (the reason django-upgrade rejected it in 2021) is stale.
- **Format preservation**: the design center. Lossless CST — comments, whitespace, parens all
  round-trip; `node.with_changes(...)` leaves untouched formatting byte-identical. This is the
  only maintained library that clears §10's "reviewable diff" bar for structural edits.
- **Structural matching**: `libcst.matchers` gives declarative structural patterns
  (`m.Call(func=m.Attribute(...))`), composable with visitor dispatch via `@m.call_if_inside`,
  `@m.leave` decorators. Critically, `QualifiedNameProvider` resolves *what a name actually
  refers to* — `import pytest as pt; @pt.fixture`, `from pytest import fixture as fx` — which a
  pattern-only tool (ast-grep) cannot do. `ScopeProvider` gives assignments/accesses per scope,
  needed for "is this parameter name shadowed locally" and for the §5 traps.
- **Decorators / parameters / defaults**: first-class. `FunctionDef.decorators` is a list of
  nodes; `Parameters`/`Param.default` model exactly the `Depends()`-in-default-position rewrite
  velox needs. Prior art: JelleZijlstra's
  [autotyping](https://github.com/JelleZijlstra/autotyping) does parameter-level signature edits
  with LibCST in production.
- **Imports**: `libcst.codemod.visitors.AddImportsVisitor` and `RemoveImportsVisitor` are
  precisely the "add this import idempotently, merge into existing from-imports, don't duplicate"
  machinery §10 needs; battle-tested by bump-pydantic and django-codemod.
- **Cross-file**: `FullRepoManager` + `FullyQualifiedNameProvider` compute repo-level metadata
  (module names from repo root, fully-qualified names) — the substrate for rename/move, though
  the move itself (emit target module, rewrite import sites) is application logic. Nothing on the
  market ships that logic for our shape of problem; velox's own `refactor-tools` skill exists for
  the same reason.
- **Codemod framework extras that map 1:1 onto §10**: `libcst.tool codemod` / 
  `parallel_exec_transform_with_prettyprint` gives multiprocess execution, **unified-diff output
  instead of writing** (dry-run for free), stdin/stdout mode, and returns
  successes/failures/skips/warnings counts — report scaffolding. `SkipFile` is the "refuse this
  file loudly" primitive the problem statement's failure-mode ranking demands.
- **Speed**: parse is Rust; tree construction/traversal is Python objects, so it is not ruff-fast.
  Reference point: django-codemod (LibCST, pure-Python-parser era) took 133 s on a 153 k-LOC repo
  vs <0.5 s for tokenize-based django-upgrade
  ([Adam Johnson](https://adamj.eu/tech/2021/09/16/introducing-django-upgrade/)). With the native
  parser and process-parallelism, expect minutes, not hours, on thousands of files. Acceptable
  for a migration tool; would be unacceptable for a linter.
- **Weaknesses**: verbose node construction when *synthesizing* code (mitigate with
  `parse_statement`/template helpers); whitespace on freshly built nodes needs care; learning
  curve for matchers + metadata; memory per process on very large files. All manageable, none
  disqualifying.

### `ast` + custom printing — fails the bar (but fine for read-only)

`ast.unparse` discards comments, blank lines, string-quote style, parenthesization — output is a
reformat of the whole file, i.e. an unreviewable diff. Fails §10 outright for rewriting. As a
**read-only** analysis substrate it is unbeatable: C parser, always current with the running
interpreter's grammar, zero deps. The only argument for using it in the audit pass is speed; the
argument against is maintaining two matcher vocabularies. On a one-shot tool, one vocabulary wins.

### asttokens splicing / isidentical-refactor

[asttokens](https://github.com/gristlabs/asttokens) (Grist) annotates `ast` nodes with source
spans; you rewrite by slicing the original text. Works for *replacing* a node's text; awkward for
inserting parameters, reordering decorators, or emitting new files — you end up hand-writing the
formatting logic LibCST already has. [isidentical/refactor](https://github.com/isidentical/refactor)
("fragmental AST refactoring") was the interesting 2022-era entrant; activity has been minimal
since ~2023 and it never grew import/scope machinery. Neither is a platform.

### tokenize-level (pyupgrade / django-upgrade style)

`ast` to detect, [tokenize-rt](https://github.com/asottile/tokenize-rt) to splice tokens.
Perfectly format-preserving, ~100–300× faster than LibCST-era codemods, always grammar-current.
But every fix is hand-rolled token surgery with offset bookkeeping; it shines for *local, shallow*
rewrites (`u''` prefixes, `super()` args, renaming a call) and becomes miserable exactly where
this migration lives: multi-decorator rewrites, inserting `Depends()` defaults into signatures
whose parameters must be reordered around `@mock.patch`, splitting a conftest into a new module.
django-upgrade (1.31.x, docs current 2026-06) is healthy and proves the model — for its problem
class, which is not ours. **Reject as the platform; steal the idempotency philosophy** (rewrites
match only the *source* form, which no longer exists after rewriting → fixpoint by construction).

### ast-grep — detection-grade, not rewrite-grade for this job

[ast-grep](https://github.com/ast-grep/ast-grep): Rust, tree-sitter-based, YAML rules + PyO3
Python bindings ([ast-grep-py](https://pypi.org/project/ast-grep-py/), 0.39.x as of late 2025,
active into 2026). Verified limitations for *rewriting*: a fix is a **textual replacement of the
single matched node** (meta-variables re-spliced, indentation-sensitive); non-recursive by
default; overlapping matches skipped; multi-node/structure-aware edits require the `rewriters`
escape hatch which is still string assembly. No qualified-name resolution (an aliased
`import pytest as pt` needs its own rule per spelling), no scope model, no import
add/remove/merge, no cross-file anything. tree-sitter's error-tolerant grammar also means it
happily "matches" code CPython would reject. It preserves comments only in the trivial sense that
it doesn't touch unmatched text. **Verdict**: viable and pleasant as a fast pre-flight
*detector* (plugin usage, `request.` sites, monkeypatch census) — but that role is already
covered by the LibCST/`ast` audit pass, and a second toolchain + rule format is pure cost. Skip.

### parso, RedBaron, Bowler, rope — mostly graveyard

- **parso** (0.8.5): alive but only as jedi's parser; error-tolerant round-trippable tree, **no
  transformation/matching/import framework**. Not a codemod platform, was never meant to be.
- **RedBaron** (on baron): dead; no meaningful release since ~2019, grammar frozen pre-3.8
  (walrus, match, positional-only all unparseable). Disqualified.
- **Bowler** (Meta, lib2to3-based): **archived 2025-08-08**
  ([repo](https://github.com/facebookincubator/Bowler)); its own README pointed at LibCST years
  earlier. lib2to3 itself was removed from the stdlib in Python 3.13, killing this whole family.
- **rope** (~1.14, maintained by @lieryan): alive, but built for interactive IDE sessions — its
  own project model, occurrence search, static object analysis; slow and fiddly in batch; its
  rewrites can reformat surrounding code. Its rename/move sounds like §4.5 but it cannot express
  "split a conftest into a fixture module and re-point N test files through a synthetic
  `velox_tests.*` namespace". Wrong platform.

### Genuinely new since 2024 — nothing that changes the answer

Verified: no new format-preserving Python rewriting library has displaced LibCST.
[codemod.com's `codemod` CLI](https://github.com/codemod/codemod/releases) is JS-ecosystem-first
(jscodeshift/ast-grep workflow orchestration + AI-assisted codemods) — orchestration, not a
Python fidelity engine. Ruff's internals (below) remain unavailable as a library. The 2026 change
worth noting is *positive for LibCST*: native parser now mandatory, 3.14/3.15 grammar landed,
free-threaded wheels — the project is healthier than in 2024.

### Ruff's fix infrastructure — why it's not reusable

Ruff's autofixes are span-based text edits attached to diagnostics, generated inside its Rust
crates against its own AST; Astral explicitly does not offer a stable public API or plugin
surface — the crates are internal, semver-unstable, and there are no Python bindings for the
transform layer. Using it would mean forking a fast-moving Rust codebase and writing rules in
Rust against internal APIs. Also, its fix philosophy (small, provably-safe edits with
applicability levels safe/unsafe) is worth copying conceptually — but the code is not extractable.

## Part B — prior-art migration tools

### bump-pydantic — the closest precedent (study frozen copy)

[Repo](https://github.com/pydantic/bump-pydantic) — **archived 2026-05-27** (pydantic v1 EOL; a
finished migration tool being archived is the expected lifecycle, and ours will share it).
Architecture worth copying:

- **Numbered rule registry** (BP001–BP010), each a LibCST codemod class, individually
  disableable (`--disable`). Maps directly onto our §5 lookup table → `VX001…` rules, plus
  §4-class rules that are analysis-heavier.
- **TODO markers**: `# TODO[pydantic]: <reason>` + docs link at every non-automatable site
  (`__get_validators__`, uninferrable field types). Exactly §10's "in-source marker naming the
  reason, greppable" — copy the format: `# VELOX-TODO[<category>]: <reason>` with a stable
  category vocabulary matching the report's sections, so report counts and grep hits reconcile.
- **Dry-run**: `--diff` prints a unified diff without writing. Copy, and make no-write the
  *default* (bump-pydantic writes by default — §10 says require an explicit flag; do better).
- **Log file** of skipped/failed transformations. Copy and extend into the full §10 report
  (counts by category, per-file line lists, %-serialized figure).
- **Idempotency**: achieved implicitly — rules match only pydantic-v1 forms, which the rewrite
  removes. Same property holds for us *except* marker insertion: re-running must detect an
  existing `VELOX-TODO` comment before inserting (bump-pydantic had duplicate-comment bug
  reports early on — test this explicitly).
- **To avoid**: no idempotency tests initially; one-shot whole-tree orientation with weak partial
  progress story; TODO comments carry no machine-readable ID for later bulk resolution.

### django-codemod vs django-upgrade — the same problem solved twice

- [django-codemod](https://pypi.org/project/django-codemod/) (LibCST): **2.5.0, 2026-08-07,
  actively maintained** — evidence a LibCST rule-per-deprecation codebase stays maintainable for
  years. Good rule organization: one visitor per deprecation, grouped by Django version, each
  independently testable via `libcst.codemod.CodemodTest` (copy that test pattern — assert
  before/after source strings verbatim, which also pins format preservation).
- [django-upgrade](https://github.com/adamchainz/django-upgrade) (ast + tokenize-rt): the
  speed-motivated rewrite of the same idea. Its README/announcement is the canonical statement of
  the trade-off (133 s → 0.5 s). Its per-"fixer" plugin architecture with token-level appliers is
  clean, but each fixer's complexity ceiling is low — its authors deliberately decline
  restructuring rewrites we cannot decline.

### unittest2pytest — the cautionary tale

[pytest-dev/unittest2pytest](https://github.com/pytest-dev/unittest2pytest): built on **lib2to3**,
dormant (last release 0.5, activity essentially frozen; conda repackaging 2024-12 is not
development). lib2to3 was deprecated in 3.9 and **removed in Python 3.13** — the tool is dead on
modern interpreters. Lesson: platform choice *is* the longevity decision; building on a parser
with an EOL date kills the tool regardless of rule quality.

### pyupgrade / flynt

[pyupgrade](https://github.com/asottile/pyupgrade) (active, asottile): fixpoint-idempotent by
construction, pre-commit-friendly, zero-config. Copy the philosophy: *every rewrite keys on
source forms the rewrite eliminates*. flynt (f-string converter, ast+token line surgery): niche,
semi-maintained; demonstrates the tokenize approach's ceiling — it has a documented history of
edge-case breakage precisely because expression rewriting via text splicing is fragile.

### pytest-specific codemods

Thin field. [expobrain/python-codemods](https://github.com/expobrain/python-codemods) has
LibCST codemods for pytest API changes;
[Alan's unittest→pytest migration](https://medium.com/alan/automated-code-migrations-our-journey-from-unittest-to-pytest-335b47cd5974)
is the best writeup — LibCST-based, migrated their suite incrementally, directory-by-directory
with the suite kept green throughout (their incremental-batches operational model matches our
"partial migration is first-class" requirement). No existing tool touches fixture *resolution*;
nobody has attempted our §4.1 — expected, since it requires a target framework to migrate to.

### Meta at scale / Fixit 2

Instagram runs LibCST codemods across a multi-million-line monorepo via the
`libcst.codemod` CLI (fork-based parallelism, formatter integration) —
[Fixit 2](https://engineering.fb.com/2023/08/07/developer-tools/fixit-2-linter-meta/) layers
hierarchical config + autofix lint rules on the same substrate. Two takeaways: (1) the
parallel-execution and diff plumbing we need already exists in `libcst.codemod` — wrap, don't
rebuild; (2) Meta's continued internal dependence on LibCST is the best available guarantee of
its maintenance through the tool's life.

## Part C — recommendation, spelled out

**One tool, one tree: LibCST for detection, rewriting, and move/rename.** The alternatives split
as follows:

- **LibCST-only** (recommended): one node vocabulary, one matcher layer; audit findings carry CST
  positions that the rewrite pass can consume directly as anchors; `QualifiedNameProvider` kills
  the alias-spelling problem once for both passes. Cost: minutes of runtime on huge suites, some
  synthesis verbosity. Confidence: high.
- **LibCST + ast-grep**: ast-grep for a fast audit, LibCST for rewriting. Buys speed the tool
  doesn't need and a slick YAML rule surface, at the price of two toolchains, two pattern
  languages, duplicated findings plumbing, and an audit layer that can't resolve names. Only
  worth revisiting if we later ship a *standalone* "can I migrate?" scanner where cold-start
  speed on 10k-file monorepos becomes a selling point. Confidence it's not needed now: high.
- **tokenize hybrid**: LibCST for structural rules, tokenize-rt for hot trivial renames. Premature
  optimization; adds a second edit engine whose edits can collide with LibCST's on the same file.
  Reject.

Pass-by-pass:

1. **Detection/audit**: pytest's own collection (plugin dump of fixture closures, param names →
   fixture defs, autouse sets, node ids) is the §4.1 authority when a working collection exists;
   the LibCST-based static scanner is both the fallback and the hazard-census engine (§6 counts,
   plugin imports, `request.` uses, monkeypatch sites). The audit emits the §10 report *and* a
   machine-readable findings file the rewrite pass consumes.
2. **Rewrite**: LibCST codemod rules, bump-pydantic-shaped registry, `AddImportsVisitor` for
   imports, `SkipFile` for refusals, `--diff`-style dry-run as default, `VELOX-TODO[...]`
   markers with existence-check for idempotent re-runs, `CodemodTest`-style verbatim
   before/after tests for every rule.
3. **Fixture move/rename**: custom pipeline — fixture graph from the audit, layout planner
   (§4.5), then LibCST emits new fixture modules (synthesized code, formatted once by the
   configured formatter — new files are exempt from the format-preservation constraint) and
   rewrites import/`Depends` sites in existing files with full fidelity. `FullRepoManager` +
   `FullyQualifiedNameProvider` for resolution; rope explicitly rejected.

## Open risks

- **LibCST governance**: Meta-driven; review latency has historically fluctuated. Current
  signal (1.9.0 in July 2026, prompt 3.15 grammar) is strong, but a Meta deprioritization would
  leave us on a fork. Mitigation: the tool's LibCST surface is wide but standard (codemod,
  matchers, metadata) — pinnable for the tool's whole life since it's not a long-lived service.
- **Grammar lag on brand-new Python**: LibCST grammar support lands months after CPython
  releases (3.14/3.15 landed in 1.8.x/1.9.0). A target suite using bleeding-edge syntax
  (e.g. new t-string forms) days after a CPython release may not parse. Acceptable: migration
  targets are 3.10+ suites, not day-one adopters.
- **Synthesized-code formatting**: freshly built CST nodes need whitespace discipline; the plan
  (run the user's formatter over *new* files only, never over edited ones) must be enforced in
  code, or reviewability degrades exactly where §4.2 chain specialization emits the most code.
- **Idempotency of marker insertion** is not free (bump-pydantic's duplicate-TODO history);
  needs first-class tests: run every rule twice over every fixture corpus, assert byte-identical.
- **Memory/time on pathological files** (10k-line parametrize tables): per-file cost is
  LibCST-object-heavy; the multiprocess pool amortizes across files but not within one. Measure
  early on a real large suite (e.g. run the audit pass over pandas' or Django's test tree).
- **No prior art for §4.1/§4.2**: everything above validates the *mechanics* platform; the
  fixture-resolution and chain-specialization passes are novel. The library choice neither
  solves nor blocks them — but it means effort estimates can't be calibrated against any
  existing tool.
