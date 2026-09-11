# Affected-test selection ("testmon")

Re-run only the tests a source change can reach, instead of the whole suite.

Status: **awaiting a decision**, not started. ROADMAP lists this under "Needs human review — do
not start"; this document is the assessment that gate was waiting for. Nothing here is
implemented.

Two prototypes were run against the real interpreter to settle the questions that decide
viability; both are recorded under "Prototype results" below.

## The decision to make first

Every existing cache in voci obeys one rule, stated in `spec/03-discovery-and-collection.md` §6
and repeated in `_cache.py` and `index.py`: **the cache only ever orders and predicts, never skips
a test.** `--lf` narrows discovery but a stale entry costs an import, never a wrong report. The
collection index is never proactively invalidated because a stale entry is always safe.

This feature breaks that rule. It skips tests on the strength of a recorded map, and when the map
is wrong in the under-approximating direction the result is a green run that should have been red.
That failure is silent, it is indistinguishable from a real pass, and it lands on the one signal
the tool exists to produce.

So the decision is not "is this buildable" — it is buildable, see below. The decision is whether
voci wants a mode whose failure mode is a false green. If yes, the containment is: opt-in per
invocation, never a default, never inherited from `[tool.voci]` alone, and a loud line in the
report saying how many tests were skipped and on what basis.

**Recommendation: build Option A (static import graph) first.** It over-approximates, which is the
safe direction — it can only run too many tests, never too few — and it needs no tracing, no
per-test map, and no runtime cost at all. Option B (tracing) is a precision upgrade on top, and
its own risks are much easier to judge once the coarse version is in and its selectivity has been
measured on a real suite.

## Work to do

Milestones are alternatives, not a sequence — pick one, per the decision above.

### Option A — static import graph (recommended first step)

Over-approximating, no runtime instrumentation, no attribution problem.

- [ ] Module graph builder: parse each discovered file's imports (AST, no execution), resolve to
      first-party modules under the rootdir, build the transitive closure per test file.
- [ ] Fingerprint every first-party module by content hash; store the graph and hashes in
      `.voci_cache/` alongside the collection index, same schema-version and atomic-replace
      discipline as `_cache.py`.
- [ ] Selection: a test file is affected when any module in its closure changed, plus the bail-out
      set below. Reuse the `lastfailed.candidate_files` seam so discovery never imports a file the
      run has no intention of running.
- [ ] Union with last run's failures — a recorded failure always re-runs regardless of the graph.
- [ ] Report line: how many tests were selected, how many skipped, and why a full run happened
      when it did.

Granularity is the whole test *file*, since that is what an import graph resolves to. Cost is one
AST parse per file, already paid by collection.

### Option B — traced per-test dependency map

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
4. **Non-Python inputs.** Fixture data files, templates, `.env`, schema files — nothing traces
   them, and a change to one must either force a full run or be declared. testmon has this hole
   too.
5. **Environment drift.** An upgraded dependency, a different interpreter, a changed
   `[tool.voci]`, a different `-k`/`-m` — each invalidates the map wholesale. This is the
   "bail-out set", and it must be conservative and fingerprinted, not inferred.
6. **Assertion rewriting.** Fingerprints keyed to anything but source text will churn or, worse,
   fail to churn when they should.
7. **Cache growth.** A per-test function map on a large suite is orders of magnitude bigger than
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
