# Affected-test selection ("testmon")

Re-run only the tests a source change can reach, instead of the whole suite.

Status: not started, pending a go/no-go. ROADMAP lists this under "Needs human review — do
not start"; this document is the assessment that gate was waiting for. Nothing here is
implemented.

Two prototypes were run against the real interpreter to settle the questions that decide
viability; both are recorded under "Prototype results" below.

## What this changes about voci's caching

Buildable — the two questions that decided that were prototyped, see "Prototype results".

Every existing cache in voci obeys one rule, stated in `spec/03-discovery-and-collection.md` §6
and repeated in `_cache.py` and `index.py`: the cache only ever orders and predicts, never skips a
test. `--lf` narrows discovery but a stale entry costs an import, never a wrong report. The
collection index is never proactively invalidated because a stale entry is always safe.

This feature departs from that. It skips tests on the strength of a recorded map, and a map that
under-approximates produces a green run that should have been red — a failure that is silent and
indistinguishable from a real pass.

That is a property of the feature class, not an argument against it; pytest-testmon ships with it
and is widely used. It does mean the cost of a wrong answer here is different in kind from the
cost of a wrong `--lf`, which is worth pricing deliberately rather than by analogy to the existing
caches.

Exposure is adjustable, and the levers are independent: which invocations can select (flag only,
or also `[tool.voci]`), whether a skipped test is reported and how loudly, and how wide the
bail-out set is — the conditions that force a full run regardless of the map. Where those land is
a call about how much risk is worth the time saved, which depends on how the suite is used.

**Recommendation: Option B (tracing), if this is built at all.** An earlier draft recommended
Option A first, on the grounds that it over-approximates and so fails safely. Measuring its
selectivity withdrew that: on `oss/fastapi` the median source change selects **56% of the suite**,
because a re-export hub gives every importer of the package a dependency on everything behind it
(see "The hub problem"). A 2x reduction does not pay for a new config surface, a new AST pass over
first-party source, and a resolution rule that has to handle `src/` layouts, workspaces and
non-editable installs.

Option A's safety argument still stands and its failure direction is still the better one. It is
the selectivity that does not hold up, and a selection feature that rarely deselects is not worth
its own maintenance.

Option B's attribution mechanism is prototyped and works, and the hub problem does not apply to it:
importing a module is not executing it. Its exposure to under-approximation is real and is
enumerated below — much of that list applies to both options.

The third answer is **don't build it**, and it is not a weak one. `--watch` plus `--lf` already
covers the fast inner loop, which is where most of the value is.

## Work to do

Milestones are alternatives, not a sequence.

### Option A — static import graph

Over-approximating, no runtime instrumentation, no attribution problem.

- [ ] **Decide what "first-party source" means, and where it is configured.** voci has no such
      concept today: `testpaths`, `ignore` and `test_file_patterns` are all test-side, and
      `Config.source` is the path of `pyproject.toml`, not a source root. The graph cannot be
      built without one, so this is the first task, not a detail — see "The source-root gap".
- [ ] Module graph builder: parse each first-party file's imports (AST, no execution), resolve
      module names to files, build the transitive closure per test file.
- [ ] Fingerprint every first-party module by content hash; store the graph and hashes in
      `.voci_cache/` alongside the collection index, same schema-version and atomic-replace
      discipline as `_cache.py`. Reparse only the files whose hash moved.
- [ ] Selection: a test file is affected when any module in its closure changed, plus the bail-out
      set below. Reuse the `lastfailed.candidate_files` seam so discovery never imports a file the
      run has no intention of running.
- [ ] Union with last run's failures — a recorded failure always re-runs regardless of the graph.
- [ ] Report line: how many tests were selected, how many skipped, and why a full run happened
      when it did.

Granularity is the whole test *file*, since that is what an import graph resolves to.

**Cost is a new pass, not a free one.** The assertion rewriter parses every `.py` under the *test
roots*, so test files and their helpers are already parsed — but first-party source is only ever
imported, never parsed by voci, and that is most of what this graph is made of. Measured
`ast.parse` + import walk over whole trees:

| tree | files | LOC | cold parse | warm (stat + hash) |
| --- | --- | --- | --- | --- |
| `voci/` | 60 | 17k | 79ms | 2ms |
| `oss/rich` | 213 | 52k | 250ms | 2ms |
| `oss/pytest` | 352 | 151k | 715ms | 5ms |
| `oss/marshmallow` | 1309 | 458k | 2.3s | 18ms |

Cold build is seconds on a large tree. Steady state is the warm column, because an unchanged file
is a hash comparison and never a reparse — the same fingerprinting discipline `index.py` already
applies to collection.

### Option B — traced per-test dependency map (recommended, if building)

Function-level precision. Everything in Option A's bail-out set still applies.

- [ ] Collector: a `sys.monitoring` tool (id 3 or 4 — 0/1/2/5 are reserved for debugger,
      coverage, profiler, optimizer) on `PY_START`, whose callback reads a `ContextVar` holding
      the running test id and records `(filename, qualname)`.
- [ ] Set that `ContextVar` in `_run.run._run_one`'s envelope so setup, call and teardown are all
      attributed; `ContextPropagatingExecutor` already carries it into the threads sync tests run
      on, so no change is needed there.
- [ ] Static fixture union: for each record, add the source fingerprint of every `Fixture._func`
      reachable through `record.plan.steps`. This is what closes the shared-scope hole — a
      `scope="session"` fixture executes once and traces only to the first test that triggered it,
      but voci knows the whole graph statically and does not have to discover it by tracing.
- [ ] Fingerprints over *source text* per function (`qualname` -> hash), not line numbers and not
      code objects: assertion rewriting changes both, and an edit elsewhere in a file shifts every
      line below it.
- [ ] Storage: per-test sets are much larger than the existing caches. Measure before choosing
      JSON; sqlite is the likely answer, as it is for testmon and coverage.py.
- [ ] `@voci.isolated`: the subprocess writes its map into the existing result JSON
      (`isolated.result_to_json`) and the parent merges — same shape as the coverage `harvest`
      path already carrying data across that boundary.
- [ ] Decide the measurement mode (see "The DISABLE race" below).

Option B needs no source-root configuration: `code.co_filename` names the real file at runtime, and
"first-party" is decidable from it directly — under the rootdir, not under `site-packages`. The
file set discovers itself.

### Option C — line/block level, true testmon parity

Not recommended. `LINE` events measured 1.73x on a call-heavy loop, the mapping from executed
lines back to source blocks has to survive assertion rewriting, and the precision gain over
function-level granularity is small for the complexity it costs.

## Prototype results

Both probes were run on CPython 3.14.7 and are the reason this is judged buildable.

**Per-test attribution under concurrency — solved.** This was the open question: voci runs at
concurrency 16 by default, async tests interleave on one event loop thread and sync tests run in a
shared executor pool, so nothing about a thread or a stack frame identifies which test is running.
A global `sys.monitoring` callback that reads a `ContextVar` attributes correctly anyway, because
asyncio enters each task's own context before stepping it and `ContextPropagatingExecutor` copies
that context into worker threads. Three tests — two interleaved async, one sync in the pool, each
calling a different function — were attributed with no cross-contamination and no serialization.

**Overhead, on a deliberately call-heavy microbenchmark** (worst case for `PY_START`; a real suite
spends far more time in I/O, imports and C code where these events never fire):

| mode | overhead |
| --- | --- |
| `PY_START`, every call | 2.56x |
| `PY_START`, `DISABLE` after first hit | 1.03x |
| `LINE`, every line | 1.73x |

### The DISABLE race

`DISABLE` makes tracing nearly free by retiring each code location after its first hit, and
`sys.monitoring.restart_events()` re-arms everything between tests, which preserves per-test
attribution — verified: two tests calling one shared function both recorded it.

It is **unsound at concurrency > 1**. `restart_events()` is global and interleaving is not: test A
hits a function and retires it, test B — already started, its own restart long past — calls the
same function, receives no event, and never records the dependency. The map silently
under-approximates, which is exactly the failure that turns into a false green.

So the measurement run is a choice between:

- **concurrency 1 with `DISABLE`** — near-free tracing (1.03x), but a serial run, which gives up
  voci's main advantage for every run that refreshes the map.
- **full concurrency without `DISABLE`** — parallel, correct, and pays up to 2.56x on call-heavy
  code, less on anything realistic.

The second is the better trade. The first is worth keeping as an explicit flag for a cold build of
the map on a large suite.

## The source-root gap

Option A resolves import statements to files, which means it has to know which modules are
first-party and where they live. Nothing in `Config` says. A source layout can be a flat package
beside `tests/`, a `src/` layout, several packages in a workspace (this repo has two), or a
package installed non-editable, where the imported module resolves into `site-packages` and an
edit to the working tree does not affect the run at all.

The non-editable case is the sharp one: a graph built over source files the tests are not actually
importing selects confidently and wrongly, with no symptom to notice. Detecting it is cheap
(compare the imported module's resolved file against the source root); what to do about it —
refuse, warn, or fall back to a full run — is a judgement call.

So Option A's first task is a config surface plus a resolution rule, and its "cheap" billing reads
differently with that included. Option B sidesteps this entirely, which narrows the gap between
the two more than the original recommendation allowed for — Option A is still the safer direction
because it over-approximates, but it is not the free one.

## Failure modes

Two directions, with very different costs. **Under-approximation** — a real dependency the graph
cannot see — skips a test that should have run, and reports green. **Over-approximation** — a
dependency that is in the graph but not in reality — runs tests that did not need to, costing time
only.

### Under-approximation: dependencies with no import statement

- **`mock.patch` targets are strings.** A test patching `"myapp.services.client"` depends hard on
  that module with no import naming it. voci already extracts these statically —
  `_mocking.patching_of` returns `targets: tuple[str, ...]` — so they can be resolved and unioned
  into the closure. This one is mitigable, and voci is better placed to do it than a plugin would
  be.
- **Dynamic imports.** `importlib.import_module(name)`, `__import__`, entry points, plugin
  registries, anything assembling a module name at runtime. No static scan resolves these.
- **Framework string references.** Django `INSTALLED_APPS`, SQLAlchemy registry names, pydantic
  forward refs, any "dotted path in a config value" pattern.
- **`voci.use(...)` on a package `__init__.py`.** The fixture is an imported object, so the
  declaring module's own imports are visible — but only if the closure includes the enclosing
  `__init__.py` chain (`requires.package_inits`). Miss that and a test loses a dependency it never
  names.
- **Subprocesses**, including `@voci.isolated`, whose imports happen somewhere the parent's graph
  is not looking.

### Under-approximation: dependencies that are not Python

Nothing here is reachable by parsing imports, and each needs either a declared association or a
rule that forces a full run:

JSON/YAML/TOML fixture data · golden and snapshot files · SQL schema and migrations · Jinja and
HTML templates · `.env` files · certificates and keys · binary assets · generated code, where the
real dependency is the generator's input rather than its output.

### Under-approximation: state outside the file tree

- **`os.environ`** from the shell — invisible, and frequently what a test's behaviour turns on.
- **`[tool.voci]` itself.** `env` injects environment variables, `filterwarnings` changes which
  warnings are errors, `timeout` and `concurrency` change what fails — `concurrency` especially,
  since it decides whether a race surfaces at all. A config edit changes outcomes with no `.py`
  file touched.
- **Installed dependencies.** A bumped version in `uv.lock` or a mutated `site-packages` is a real
  behaviour change; the graph is rooted in first-party code and never sees it.
- **Interpreter version.** voci supports 3.13 and 3.14 and they differ where it matters — PEP 649
  annotations, warning filters. A map built under one is not valid under the other.
- **Compiled extensions**, rebuilt with no `.py` change.
- **Ambient machine state**: locale, timezone, platform, CPU count.
- **External services**: database schema and seed data, network fixtures, container images.

### Under-approximation: test-side semantics

- **`skipif` conditions** are evaluated per run, so the set of tests that would run can change with
  no file change at all.
- **Parametrization over dynamic data** — cases read from a file, env, or a database — changes
  both the case set and the ids that the map is keyed on.
- **Order dependence**: a test that passes only because another ran first has a dependency the
  graph has no way to express.

### Over-approximation: cheap to be wrong, expensive to be useless

- **mtime churn.** A branch switch or rebase rewrites mtimes without changing content. Content
  hashing makes this a non-event; `(mtime, size)` alone does not.
- **`if TYPE_CHECKING:` imports** are in the graph and absent at runtime.
- **Re-export hubs** — the one that decides whether this feature is worth building.

### The hub problem

A package `__init__.py` that re-exports its submodules gives every importer of the package a
dependency on all of them. Import-graph selection then degenerates, because import granularity
cannot distinguish "imported the package" from "used this part of it".

Measured on `oss/fastapi` — 593 test modules, 48 `fastapi/` modules, transitive closure per test
file, asking what fraction of the suite a change to each source module selects:

| | |
| --- | --- |
| median | **56.0%** |
| mean | 38.3% |
| modules selecting >50% of the suite | 32 / 48 |
| modules selecting <10% of the suite | 16 / 48 |

The clustering at exactly 56.0% is the hub: `fastapi/__init__.py` re-exports `routing`,
`responses`, `requests`, `websockets`, `exceptions` and the rest, and 56% of test modules reach
that hub, so every module behind it inherits the hub's entire fan-in. Changing `fastapi/routing.py`
reruns 56% of the suite; so does changing `fastapi/types.py`.

So on a hub-style package — which is the normal way to lay out a Python library — Option A buys
roughly a 2x reduction, not the 10x the feature is usually sold on. On a suite whose tests import
deep modules directly it does much better, and this repo's own `tests/` mostly import `voci._*`
submodules rather than the `voci` hub.

**This is specific to import granularity, and Option B does not share it.** Importing a module is
not executing it: a test that imports the `fastapi` hub but only ever calls three functions in
`routing` depends, under tracing, on those three functions. The hub collapses the graph precisely
because it is a static relation over files rather than a record of what ran.

## Risks

Ranked by how badly each one ends.

1. **Silent under-approximation.** The whole feature class. See "The decision to make first".
2. **Import-time and module-level code.** Executes once for the first test that imports the
   module and never again, so tracing attributes it to that test alone. Option A does not have
   this problem; Option B needs module-level edits treated as whole-file invalidation.
3. **C extensions and dynamic dispatch.** `PY_START` fires for Python frames only, so a
   dependency that lives behind a C extension is invisible. `getattr`-driven dispatch, plugin
   registries and metaclass machinery record the frames they actually ran, not the ones a changed
   input would have selected.
4. **Source-root misresolution (Option A).** A graph built over files the tests are not really
   importing — a non-editable install, a shadowed package name, a path the resolution rule guessed
   wrong — selects with full confidence and no symptom. Cheap to detect; the response is a
   design choice.
5. **Non-Python inputs.** Fixture data files, templates, `.env`, schema files — nothing traces
   them, and a change to one must either force a full run or be declared. testmon has this hole
   too.
6. **Environment drift.** An upgraded dependency, a different interpreter, a changed
   `[tool.voci]`, a different `-k`/`-m` — each invalidates the map wholesale. How wide this
   "bail-out set" is trades directly against how often the feature helps.
7. **Assertion rewriting.** Fingerprints keyed to anything but source text will churn or, worse,
   fail to churn when they should.
8. **Cache growth.** A per-test function map on a large suite is orders of magnitude bigger than
   `lastfailed.json`.

## Where it pays off

`--watch` is the strongest case: the loop is already red, fix, rerun, and it already applies `--lf`
automatically from the second run on. Affected-test selection is the same idea extended to the
green case — after a passing run, an edit currently reruns everything. That is the gap worth
closing, and it is also the lowest-risk place to put the feature, since a `--watch` session is
interactive, short-lived, and always followed by a real full run.

## References

- `voci/_cache.py` — schema versioning, atomic replace, and the merge discipline any new cache
  here should copy.
- `voci/_collection/index.py` — `(mtime_ns, size)` fingerprinting, and the "never proactively
  invalidate" rule this feature cannot keep.
- `voci/_collection/lastfailed.py` — `candidate_files` is the discovery-narrowing seam.
- `voci/_run/coverage.py` — the existing pattern for carrying measurement across the
  `@voci.isolated` boundary and merging it back.
- `voci/_builtins/capture.py` — `ContextPropagatingExecutor`, which is what makes `ContextVar`
  attribution work for sync tests.
