# Affected-test selection

Re-run only the tests a change can reach. A `sys.monitoring` tracer records, per test result, the
first-party functions, data files and environment variables it touched. The next run re-runs a
test only when one of those no longer matches the tree.

Status: design settled, not started. The final milestone replaces today's `--watch` with one
built on this. Reference implementation: `oss/pytest-testmon`.

## Work done

- Prototypes. ContextVar attribution is correct under concurrency. A static import graph was
  rejected: the median `oss/fastapi` change selects 56% of the suite, because
  `fastapi/__init__.py` re-exports everything. Line-level tracing was rejected at 1.73x for
  little gain.
- Research pass, 2026-09-12: pytest-testmon's source, docs and issues, voci's seams, and probes.
  Findings are folded into Design and Failure modes. The prototypes and full reports are in
  [research/affected/](../research/affected/README.md), indexed by the plan section each one
  backs.
- User docs drafted early, from the settled design, for review before implementation:
  [docs/guide/affected.md](../docs/guide/affected.md), the "Affected-test selection" section of
  [docs/reference/cli.md](../docs/reference/cli.md), and the `affected_trace_threads`/
  `affected_trace_subprocesses` entries in [docs/guide/config.md](../docs/guide/config.md). M3,
  M6 and M8 below reconcile these with whatever implementation changes.
- Probed which async file/DB primitives propagate the caller's context into their worker thread:
  `asyncio.to_thread` and `anyio.to_thread.run_sync` do, on their own; `loop.run_in_executor`
  does not, at all, which is what `aiofiles` calls internally. Folded into Tracer, Non-code
  dependencies and Failure modes.
  [research/affected/probes/async_file_io_context.py](../research/affected/probes/async_file_io_context.py).

## Work to do

Milestones are in build order. `--affected` stays hidden until M4 lands: without the non-code
dependencies, selection could skip a test it shouldn't.

**M1 — Recording** (see Tracer)

- [x] `voci/_affected/tracer.py`: tool id, callback, first-party classification. Nothing calls
      `Tracer` yet -- the next bullets wire it to a collector.
- [x] `voci/_affected/collector.py`: `Collector` + a `current_collector` ContextVar, nested the
      way `_capture.current_test_context` is. Wired at the three call sites -- a test's own
      (`_capture.TestContext.collector`, set in `run.py:_Session.run_envelope`), a
      `module`/`session`-scope fixture's own, shared between construction and teardown
      (`_di.runtime._Entry.collector`), and one per file import
      (`CollectionResult.collectors`, keyed by resolved path). Still nothing starts a real
      `Tracer` for a run, so nothing is recorded into any of these yet.
- [x] Untrusted marking for unattributed first-party code (`collector.py`); opt-in
      `affected_trace_threads` (`_affected/threads.py`, patching `Thread.start` and
      `loop.run_in_executor`); `@voci.untrusted(reason)` (`_marks.py`) for a test to declare it
      manually. None of the three is wired into a real run yet.
- [x] `@voci.isolated`: the worker starts its own `Tracer` (`_isolated_worker.py`, one per
      subprocess -- a fresh interpreter shares no tool id or ContextVar with the parent) and ships
      what it saw back as a `CollectorRecord` through `result_to_json`'s `collector` key
      (`isolated.py`), the way coverage's `harvest` carries measurement data across the same
      boundary. `run_suite` gained `on_collector` (`run.py`) to get a test's finished collector
      out of `run_envelope` either way, isolated or not. Nothing yet resolves a `CollectorRecord`
      into dependency keys or stores it -- that's M2.

**M2 — Fingerprints and store** (see Fingerprints, Storage)

- [x] `voci/_affected/blocks.py`: statement and def blocks, with binds, references and effects,
      from one parse per file.
- [x] `voci/_affected/resolve.py`: `World.closure` (name closure, string index, whole-module
      fallback -- rules 2, 4, 5) and `World.effect_fold_target` (rule 3), as two separate
      queries rather than one BFS: a checksum's own effect-fold is a property of the *key*
      (computed by whoever hashes it), never of one test's closure, so `closure` never calls
      `effect_fold_target` itself. Soundness cases from "Probe results" are
      `tests/affected/test_resolve.py`. `blocks.py`'s `_collect_references` gained nested-import
      references (a real gap: it only walked `Name` loads) along the way.
- [x] `code → block` resolution at session end: `World.resolve_code` (`resolve.py`) maps one
      `CollectorRecord` code identifier to seed keys; `seeds.py`'s `seeds_for_record` drives it
      per record and drops one outright if it touched a file that changed mid-run.
- [x] `voci/_affected/store.py`: location, schema, parse cache, checksum computation
      (`Fingerprints`, `module_checksum`) and `store_record`. Sizing on voci's suite and httpx2
      after 20 branch switches is M7's job.

**M3 — Selection and CLI** (see Selection)

- [x] `voci/_affected/select.py`: `decide`'s per-test rule and the `Selection` it feeds
      `candidate_files`/`select` from, mirroring `lastfailed`'s own two-step shape over the store
      instead of `LastRun` (`_collection/collect.py` gained the two bits both share,
      `path_of_test_id`/`reindexed`). `store.py` gained the read side, `load_records`, one joined
      query per environment. Nothing calls any of this yet -- that's the session-start/end driver,
      next. `candidate_files`'s own soundness gap (a new test beside only-`SKIP` siblings) is in
      Failure modes.
- [ ] `--affected` and its sibling `--affected-verify`. Both are wired end to end and working,
      `cli.py`, `help=argparse.SUPPRESS`-hidden until M4: `main` opens the store, builds a
      `World`, and computes a `Selection` before collection (`_prepare_affected`), opens a real
      `Tracer` around collection and the run (`tracing.traced`), and -- for plain `--affected`
      only, gated on `session.verify` -- narrows `candidate_files`/`select` the same way `--lf`
      does. Both modes record every finished test's dependency closure afterward
      (`_execute_suite`'s `on_test_dependencies`, bound through `_record_test_dependencies` to
      `driver.record_test`); `--affected-verify` additionally checks `driver.verify_prediction`
      against each real outcome as it's known and prints a `MISMATCH` block plus a `would have
      been skipped` count (`_report_verify`). A Tracer that can't claim a tool id disables
      narrowing and recording/verifying alike for that run (`session.affected` stays unset)
      rather than risk storing a vacuous, always-matching dependency set -- the "no tool id is
      free" full run reason exists for exactly this. `tests/test_cli_affected.py` covers both
      flags end to end: a first run, an unchanged second run skipping/verifying its only test, a
      changed test rerunning, a still-failing test always rerunning, a genuine `--affected-verify`
      mismatch (an env var flips a test's outcome without its code changing), the store's on-disk
      location, and every new-flag usage error (`--watch`, `--lf`, combining the two siblings).
      Still needed: the `N selected · M unaffected` summary line for plain `--affected`
      (`select.select()` currently folds an unaffected test into the same `deselected` count `-k`/
      `-m` use, so a fully-skipped run exits 5 same as a genuinely empty suite -- distinguishing
      the two needs a count carried alongside `CollectionResult`, not read back out of it); and
      unhiding the flags once M4 lands. Matches the draft in `docs/reference/cli.md` and
      `docs/guide/affected.md`; regenerate the former's generated block afterward.

**M4 — Non-code dependencies** (see Non-code dependencies, Environment key)

- [ ] Audit hook, `os.environ` recorder, environment key.

**M5 — Starlette/FastAPI adapter** (see Starlette/FastAPI adapter)

- [ ] `voci/_affected/adapters/starlette.py`: request recorder, matched-route capture, route
      table extraction, and the enumeration hooks.
- [ ] Static route parser, prefix derivation, and claimed effects in `resolve.py`.
- [ ] Selection rules. Port the probe's 16 edits as tests against a synthetic app, checking that
      the changed tests are a subset of the predicted ones.

**M6 — Child-process tracing, opt-in** (see Child processes)

- [ ] `[tool.voci] affected_trace_subprocesses = true`, off by default. Setting it without the extra
      installed is a config error that names the extra. Matches the draft in
      `docs/guide/config.md`.
- [ ] A `voci-subprocesses` workspace distribution, installed as `voci[subprocesses]`. It holds
      only the `.pth` and the child bootstrap. Check that the `.pth` lands in both a regular
      install and an editable one.
- [ ] Parent side, active only with the key set and only for the duration of a run: patch
      `subprocess.Popen` and `multiprocessing.process.BaseProcess.start`, add the fork hook, sort
      spawns into traced and untrusted, and merge child files at session end.
- [ ] Tests for every row of the child table below, on 3.13 and 3.14, including two concurrent
      tests spawning children at the same time.

**M7 — Measure before recommending it**

- [ ] Overhead on voci's suite and httpx2, alone and with `COVERAGE_CORE=sysmon` coverage.
- [ ] Replay ~200 commits each of `oss/fastapi`, `oss/httpx`, and a real FastAPI *application*
      whose tests run without external services, added as an `oss/` submodule (fastapi's own
      suite tests the library, not an app). Run each under `verify` and record the fraction
      selected, which rule selected each test, and any false greens.
- [ ] Branch churn: on each corpus, alternate among 3 branches 20 times, one of them with a
      lockfile change. After the first visit to each tree, a return should select nothing, and
      a switch should select only the tests whose dependencies differ from every stored state.
      Every full run is a bug to explain.

**M8 — Replace `--watch`** (see `--watch`)

- [ ] Delete `voci/_watch.py`, `cli._watch_scope`, the in-process re-invocation wiring in
      `cli.main` (the `wall_start` parameter and the argv filtering), and `tests/test_watch.py`.
- [ ] New parent loop, watched set, and debounce.
- [ ] Measure per-iteration startup on httpx2. Add the warm fork mode only if third-party
      imports dominate.
- [ ] Regenerate `docs/reference/cli.md`'s generated block for the new `--watch`, reconcile it
      and `docs/guide/affected.md` with whatever changed since the draft, and update
      `spec/02-cli-and-config.md`.

## Decisions

- **Function-level tracing.** Each test is checked against its own stored records, never against
  what changed since the last run, so partial runs, interrupted runs, and `-k`/`-m` cost extra
  re-runs but never a missed test (testmon bugs #78, #204).
- **Branch switching among a handful of branches all day is the main workload,** not an edge
  case: a test's result is cached by the content of its dependencies, like a build cache, not by
  what the previous run saw. Mechanism: Selection, Environment key, Storage; M7 measures it.
- **A pass skips a test only when it's the most recent matching record;** failures, errors,
  timeouts, skips, untrusted tests, and new tests always run (see Selection).
- Comment and whitespace edits invalidate nothing. Docstring edits do, since `__doc__` is read
  at runtime.
- **`--affected` and `--affected-verify` are flags only,** with no `[tool.voci]` key for either.
- **`--affected-verify` is a sibling flag, not a value of `--affected`.** Verify is a distinct run
  mode — run everything and check predictions — not a variant reading of the same option, and an
  optional-value flag (`--affected` alone, or with `=verify`) would be a new argparse pattern this
  CLI doesn't otherwise use. `-x`/`--maxfail`, `--lf`/`--ff`, and pytest-testmon's own
  `--testmon`/`--testmon-forceselect` are all separate flags for the same reason.
- **No surprise patching by default.** Anything that changes behaviour user code could observe
  is an opt-in `[tool.voci]` key: `affected_trace_subprocesses` (`Popen`, `multiprocessing`) and
  `affected_trace_threads` (`Thread.start`, and `loop.run_in_executor` for the sake of `aiofiles`
  and other direct callers). Both are flat keys, prefixed rather than nested under a new
  `[tool.voci.affected]` table, since nothing else in `[tool.voci]` nests. Off, the affected
  tests are simply untrusted; the default path only observes, so code sees no difference in
  values or types (see Tracer, Non-code dependencies).
- **A test can assert its own untrusted status:** `@voci.untrusted(reason)`, alongside the
  automatic marking from an unattributed thread, subprocess, or dynamic import. It always runs
  under `--affected`, and it never produces a skip prediction for `--affected-verify` to check
  against, so a known gap doesn't need re-discovering on every `verify` run (see Tracer).
- **`--watch` is rebuilt on `--affected`, not patched.** Today's `--watch` runs `cli.main`
  in-process and evicts only test modules, so an edited first-party module keeps running its old
  code (probed), and it polls only the test roots. The replacement starts with the tests that
  need running, then keeps re-running failures plus whatever each change affects (see
  `--watch`).
- **Child processes are untrusted by default:** a spawn marks the test untrusted via the audit
  hook, and nothing is patched. Tracing a bounded subset — same-interpreter `subprocess` and all
  three `multiprocessing` start methods — is an opt-in (`affected_trace_subprocesses`, M6), shipped as a
  separate `voci[subprocesses]` extra so a base install runs nothing extra at interpreter start.
  It excludes abrupt-exit patching, signal handlers, and foreign interpreters, where coverage.py's
  subprocess bug history concentrates (#310, #1101, #1892, #2137) (see Child processes).

## Design

### Tracer

- **Tool id, callback and first-party classification:** built, `voci/_affected/tracer.py`.
- **Recording:** first-party code goes into the innermost collector, keyed by `id(code)` with a
  keep-alive dict, since hashing code objects is slow. `restart_events()` is never called.
- **Which collector:** the test's collector covers setup, call, teardown and function-scope
  fixtures. A `module`/`session` fixture gets its own collector around construction *and*
  teardown. Session teardown runs in `store.aclose()` with no test context (`run.py:1345`).
- **Collection:** each test file's import has a collector, which catches module bodies,
  decorator application and class creation.
- **Threads:** neither 3.13 nor 3.14 passes context to new threads (probed).
  - By default, first-party code that runs with no collector while tests are in flight marks
    those tests untrusted. It's safe, and it's cheap for FastAPI: anyio copies the calling
    test's context into `TestClient` handler calls, including through a persistent portal
    (probed), so those need no help.
  - Opt-in `[tool.voci] affected_trace_threads = true` patches `Thread.start` for the duration
    of a run, so the target runs in the creator's context (`Thread(context=)` on 3.14, a wrapped
    `run` on 3.13). A server thread started by a session fixture then reports to that fixture.
  - It's opt-in for the same reason as `affected_trace_subprocesses`: threads can observe the
    change, because they see their creator's ContextVars.
- **Async offload:** `asyncio.to_thread` and `anyio.to_thread.run_sync` (so `anyio.Path` too)
  copy the caller's context into the worker thread on their own, per call, needing no help
  (probed). `loop.run_in_executor` does not, at all — a thread pool worker gets a blank context
  regardless of who submitted the call (probed). `aiofiles` calls exactly that primitive
  internally (`loop.run_in_executor(self._executor, cb)`, no context copy), so its reads are
  untrusted by default like any other no-collector code, and correctly attributed once
  `affected_trace_threads` also wraps `run_in_executor` to copy the caller's context per
  submission — a narrower patch than `Thread.start`'s, since the pool's own worker threads are
  long-lived and shared across unrelated tests, not created fresh per call.
  - `aiosqlite` needs no such fix: each `Connection` runs one dedicated worker `Thread` for its
    own lifetime, queueing calls to it, so `Thread.start`'s existing creator-context capture
    already attributes every query to whoever opened the connection — a test if it opens its
    own, a fixture if the connection is shared, same as any other shared-scope resource.
- **Self-declared untrusted:** `@voci.untrusted(reason)` forces the same `untrusted=1` record
  Storage already keeps for an automatically-detected case, skipping the question of whether
  tracing agrees. Stacks with the rest of the marks; the reason is for whoever reads the test
  next, not read by voci. Because an untrusted test is always selected, it never gets a skip
  prediction to check, so `--affected-verify` reports nothing for it either.
- **Cost:** measured on a trivial function over 2M calls (3.14, WSL2, noisy). An empty callback
  costs ~1.8x, a recording one ~3x, and `DISABLE` ~1x. A dedup check doesn't help. Stdlib and
  third-party calls become free after their first hit; first-party hot loops pay ~3x.
- **Why not `DISABLE` everywhere:** for first-party code, `DISABLE` is unsound at concurrency
  > 1 (reproduced with a barrier). Re-arming it whenever the set of running tests changes is
  unsound too, because the race happens inside the window where that set stays the same.

### Fingerprints

Each block is hashed as `ast.dump(include_attributes=False)` with blake2b-8, qualname included.
Each file has:

- **Statement blocks:** one per top-level statement. Each records the names it binds, the names
  it references, and whether it's an effect (see below).
  - A `def` statement block covers the name and decorators, the part that runs at import.
  - A `class` statement block covers bases, keywords, decorators, class-level statements
    (fields, `model_config`, `__slots__`, enum members), and each method's name and decorators.
- **Def blocks:** one per function or method, covering its arguments, returns, type params and
  body. A nested def's name and decorators stay in the enclosing block; its body gets a block
  of its own.

Mapping a code object to a block:

- A def matches on `co_qualname` plus its first line (the first decorator's line, if any).
- Anything else maps to the innermost block containing `co_firstlineno`: `<lambda>`,
  `<genexpr>`, 3.14's `__annotate__`, `<generic parameters of …>`, class bodies, `<module>`.
- Comprehensions have no code object since 3.12.
- Assertion rewriting leaves qualname, line and filename untouched. The probes confirmed all
  of this.

**Records are keyed, not positional.** A record maps dependency keys to checksums, and it's stale
when any key's current checksum differs or the key has gone. The keys:

- `(path, qualname)` for def blocks. The checksum covers every def with that qualname, which
  handles redefinitions and `if/else` defs.
- `(path, name)` for module-level names. The checksum covers every statement in that module
  binding the name, plus every effect folded onto it from any file.
  - Keying on names rather than statements matters for soundness. A new `x = 2` further down a
    module leaves `x = 1` untouched but changes `x`.
  - A new `@app.get` in another file changes `app` too. Selection re-resolves the effect
    statements of changed files to find what they fold onto.
- `data:`, `dir:` and `env:` keys, from M4.

Line shifts and edits to unexecuted functions are invisible. Selection needs the parse, cached
by content, of changed files only. A cold parse plus hash of `oss/pytest/src` (39k LOC) takes
~0.5s. Don't use `ast.get_source_segment`, which measured 3–5x slower.

### What a passing test depends on

A set of dependency keys, each with its checksum. Tracing supplies the functions that ran; the
names they reach are resolved statically, per name rather than per file, so a re-export hub
costs nothing.

1. **Traced:** def blocks its own collector recorded, plus those of each `module`/`session`
   fixture in `record.plan.steps` (which already includes `voci.use(...)` fixtures). The
   test's own `def` statement block is always included; it holds the parametrize decorators.
2. **Name closure:** every name a block references resolves to the statement blocks that bind
   it, and so on transitively.
   - `from m import x` is followed into `m`'s binding of `x`, through re-exports, including
     relative and star imports (star imports resolve to `__all__` or the public names).
   - A method that ran pulls in its class block.
   - Annotations are references, including string annotations, which get parsed. So a handler
     signature reaches the pydantic models, nested models and type aliases it names.
   - Imports inside a function body are references of that def block.
3. **Effects:** a statement that mutates rather than binds folds into the binding of every
   first-party name it references. Examples: `app.include_router(r)`, `@app.get(...)`,
   `REGISTRY[k] = v`, or a call that passes a first-party name. In test files, effects fold
   only within that file. Statements claimed by the Starlette/FastAPI adapter don't fold.
   - A decorator that is a call to a first-party factory also folds onto whatever that factory's
     body mutates, following first-party calls to a bounded depth. The probe's
     `@register("key")` registry was unsound without this.
   - Every import statement (importing runs the target module) and collection-traced blocks
     (decorator bodies, metaclasses) are *module-global*: depending on anything in a module pulls
     them in.
   - An effect with no first-party target mutates process-wide state: `logging.basicConfig`,
     `warnings.filterwarnings`, `load_dotenv()`, an event-loop policy, a patched third-party
     attribute. Those are *session-global*: every test run in a process where that module was
     imported depends on them, because they apply to every test that runs afterwards. They are
     rarely edited, so this costs little.
4. **Fail safe to whole-module.** A reference the resolver can't follow (past the bound, or an
   attribute of an unknown object) becomes a dependency on the whole module where resolution
   stopped, expanded through that module's re-exports. The same happens for a module with a
   module-level `__getattr__`, `exec`/`eval`/`globals()`/`vars()` at module level, or one
   referenced bare as a value (`dir(httpx)`). It must never fall through silently.
5. **Strings:** literals shaped like identifiers or dotted paths reference every first-party
   top-level name matching any segment. String constants assigned in class bodies are indexed
   too. This covers `relationship("Address")`, `"User"` forward refs, and Django
   `"app.Model"`; `__tablename__ = "addresses"` lets `ForeignKey("addresses.id")` find its class.
6. **Runtime imports:** `importlib.import_module` is wrapped during a run. Each module a
   collector imports through it becomes a whole-module dependency. 27% of fastapi's test files
   load tutorial modules this way. A non-literal `__import__(...)` in an executed block marks
   the test untrusted.
7. **Imports resolve to keys.** Every import in the closure adds a `module:<dotted>` key whose
   checksum is where that name resolved at record time. So does every module a collector imports
   or probes at runtime (`import_module`, `importlib.util.find_spec`). The possible origins:
   - a first-party path;
   - a namespace package's directories;
   - "absent";
   - for third-party, the distribution's version plus the versions, or absence, of its
     requirement closure, extras included.

   This covers:
   - an import made to work by adding the missing file (a cherry-pick). If the `ImportError` was
     caught (`try: from app.utils import helper except ImportError: helper = None`), the test
     passed on the fallback, and nothing it depends on changes except `module:app.utils`, which
     goes from absent to a path;
   - optional dependencies (`try: import orjson`) getting installed;
   - shadowing (a new `app/json.py`);
   - third-party upgrades, per test: a pydantic bump re-runs the tests whose closure imports
     pydantic, directly or through a dependent distribution, not the whole suite.
8. `data:`, `dir:` and `env:` entries from M4.

Probe results (a static analyzer over synthetic cases, `oss/fastapi` and `oss/httpx`):

- All 21 soundness cases came out right. They covered constants, derived constants, both kinds
  of alias, and pydantic fields, constraints, `model_config` and string forward refs. They also
  covered dataclass defaults, enum members, bases, `__slots__`, registries, cross-file route
  decorators, `TYPE_CHECKING`, `try/except ImportError`, `__all__` and `del`.
- The expected misses are dynamic import by f-string, now rule 6, and table-name strings, now
  rule 5.
- Fraction of fastapi test files pulling in each of its 740 top-level statements:
  - median 54% per-file vs 0.17% per-statement;
  - a new function: 54% → 0;
  - a `_compat` constant: 55% → 0.2%;
  - `openapi/models.py` classes: 54.5% → 35%, which is real fan-in.

  Seeding came from test-file text, not from tracing, so hot-path code will select more once it
  runs for real.
- httpx median: 97% → 0%.
- Resolution over fastapi, 1,102 modules, takes ~1–1.7s cold; results are cached per content
  hash.
- No fastapi module needed the whole-module fallback. One httpx test module did (`dir(httpx)`).

### Selection

- **Current checksums** for every stored path. Content is always hashed; `(mtime_ns, size)`
  never decides a skip. Parse results are cached by content hash, not path, so a file returning
  to a version seen on another branch isn't re-parsed.
- **A test keeps several records,** one per distinct dependency state it ran under: the 8 most
  recently used, per environment.
  - The most recent record whose checksums all match the current tree decides. If it's a pass,
    the test is skipped; if it's a failure, or nothing matches, the test runs.
  - Returning to a branch whose tree was already run therefore runs nothing (testmon keeps one
    record and re-runs everything, #78).
  - A flake that failed on the same tree as an older pass still runs, because the newer record
    wins.
- **Always selected:** new tests, untrusted tests — automatically, from an unattributed thread,
  subprocess, or dynamic import, or self-declared with `@voci.untrusted(reason)` — and tests in
  files with collection errors. A record is pruned only when its id is missing from a *fully*
  collected file.
- **Added and removed files need no special rule.**
  - A removed file takes its checksums with it, and its `module:` key goes absent.
  - An added file matters only if some test's `module:` key now resolves differently. That
    covers a previously missing import, shadowing, and an `__init__.py` turning a namespace
    package into a regular one. The alternative is a listed directory (a `dir:` key).
  - Each distinct `module:` key is re-resolved once per run: first-party ones by path existence
    under the recorded import roots, third-party ones with `find_spec` on the top-level name
    plus metadata, which executes nothing.
- **Full run**, with its reason printed:
  - no stored environment key matches;
  - the store is missing or its schema has changed;
  - no tool id is free.

  Modeled on testmon's `configure.py` reasons table.

### Non-code dependencies

The audit hook is installed once per process: it can't be removed, and it's a no-op without a
collector. Probes confirmed on 3.13 and 3.14 that it sees `open`, `os.listdir`, `os.scandir`,
`subprocess.Popen` and `sqlite3.connect`. It reads the same "current collector" as the
`sys.monitoring` callback, so the async-offload findings under Tracer's Threads bullet apply here
unchanged: a file read through `aiofiles`, or any other direct `loop.run_in_executor` caller, has
no collector to attribute to by default and so falls under the same no-collector-while-in-flight
rule, making the test untrusted rather than recording nothing silently; `affected_trace_threads`
attributes it correctly once set. `asyncio.to_thread`, `anyio.to_thread.run_sync`, `anyio.Path`,
and `aiosqlite` need no such help.

- **Read-mode `open`** under rootdir → `data:<path>` = content sha. Skipped: `.py` files,
  `__pycache__`, the cache, venvs, and files modified after the session started (outputs the
  suite wrote). Opens during collection land on the file's collector, which covers parametrize
  cases loaded from data.
- **`os.listdir`/`os.scandir`** → `dir:<path>` = hash of the sorted names.
- **`sqlite3.connect`** → treated as a data file.
- **Process spawns** → the spawning collector is untrusted. The hook sees `subprocess.Popen`,
  `_posixsubprocess.fork_exec` (which `multiprocessing` calls directly), `os.posix_spawn`,
  `os.exec`, `os.system` and `os.fork`. With `affected_trace_subprocesses` on, only spawns that Child
  processes can't trace count.

`os.environ` gets swapped to a recording subclass for the run. Its `__getitem__` sees `getenv`,
`get`, `in`, `copy()` and iteration (probed). Each key read becomes `env:<KEY>` = hash of its
value, or "absent". Volatile keys (`PWD`, `SHLVL`, …) only cost re-runs.

### Environment key

A mismatch means a full run. The last 8 keys are kept, so alternating interpreters wipes
nothing; testmon deletes on every change. The key covers only what can't be attributed to a test:

- interpreter implementation, full version, ABI flags, and `sys.platform`;
- the entry points of every group queried during the run, through a pass-through wrapper on
  `importlib.metadata.entry_points`, so a newly installed plugin counts;
- the resolved `[tool.voci]`, plus `-W`, `--timeout`, concurrency and assert mode;
- `LANG`, `LC_*` and `TZ`, which C reads without going through `os.environ`;
- hashes of extension modules loaded from under rootdir.

Installed packages aren't in the key; they're per-test `module:` keys (rule 7). A lockfile
change therefore re-runs the tests whose imports reach a changed distribution:
- a dev-tool bump (ruff, pyrefly) re-runs nothing;
- a feature branch adding a dependency re-runs the tests that import it.

`DISABLE` means third-party code is never traced, and it doesn't need to be: the imports in a
test's closure name the entry points into it. First-party distributions are skipped, because
their code is tracked as source and a hatch-vcs version changes on every commit.

### Storage

- **Location:** `<git common dir>/voci/affected.sqlite3` inside a git repository, from
  `git rev-parse --git-common-dir`; `.voci_cache/affected.sqlite3` otherwise.
  - Every worktree of a repo shares one store. Records are content-addressed and paths are
    rootdir-relative, so a test that passed in one worktree needn't rerun in another on the
    same tree.
  - If git is missing or fails, fall back to `.voci_cache/`; never error (testmon #214).
- **Schema** version in `user_version`; a mismatch deletes and rebuilds the file.
- `journal_mode=DELETE`, so it stays one file. CI caches that copied only the main file lost WAL
  data (testmon #233, #236).
- Explicit close, `busy_timeout`, and rootdir-relative paths.
- One write transaction at session end, from the parent only, avoiding the concurrent-writer
  bugs testmon hit (#245, #259).
- **Tables**, modeled on testmon's `db.py:340`:
  - `env(id, key, last_used)`
  - `record(id, env_id, test_id, outcome, untrusted, last_used)`, several per test
  - `record_dep(record_id, dep_set_id)`
  - `dep_set(id, path, keyed_checksums, UNIQUE(path, keyed_checksums))`, shared across tests
    and records, so a branch variant costs only the dep sets that differ
  - `parsed(content_sha, blocks, last_used)`, the parse cache
  - adapter data: `record_request(record_id, method, path)`, the record's matched routes and
    enumerator flag, and `route_table(file, qualname, path, methods, name)` from the last run

  Least-recently-used rows are pruned at write time.

### Starlette/FastAPI adapter

FastAPI apps are the core audience. Under rule 3, every `@router.get` folds into `app`, so adding
an endpoint would re-run every test that uses the app. The adapter narrows route registrations to
the tests a route can actually reach. It's active whenever `starlette` is imported during a run.

- **Recording is observation only.** A pass-through wrapper on `starlette.routing.Router.__call__`
  (FastAPI uses the same one) records, per collector:
  - each request's `(method, path)`, from the outermost call. `scope["path"]` survives `Mount`
    nesting. The outermost-call guard lives in a ContextVar, never in `scope`, because the app
    would see an extra scope key.
  - the matched route, read from `scope["route"]` or `scope["endpoint"]` after dispatch. That
    works for 405s, for 422s where pydantic rejects the body before the handler runs, and for a
    `Depends` that raises, because Starlette writes those keys before `handle()` runs. Only a
    true 404 has no route, and its path is still recorded.

  Attribution held for module-level `TestClient`, a persistent `with TestClient(app)` from a
  fixture, `httpx.AsyncClient(transport=ASGITransport(app))` interleaved on one loop, sync tests
  in threads, and websockets. Cost is ~1.7% per request once the endpoint-to-def mapping is cached
  by `id(endpoint)`. Uncached, it was 42%, because of `inspect.getsourcelines`.
- **A matched route counts as traced.** Its endpoint's def block joins rule 1's seeds even if
  the handler never ran. So a 422 test depends on the handler signature and, through rule 2, on
  the body model's `Field(gt=...)`. No new dependency kind is needed.
- **Route table,** stored from the last run: `(file, endpoint qualname) → path with converters,
  methods, name`.
  - It's read from `fastapi.routing.iter_route_contexts()`, which flattens `include_router`
    lazily, recursing into `Mount` sub-apps, and falls back to walking `.routes` for bare
    Starlette.
  - For `Mount`/websocket entries, the real values are on the nested `starlette_route`.
- **Claimed statements.** The static parser recognises route decorators
  (`get/post/put/patch/delete/head/options/api_route/route/websocket`), `add_api_route`,
  `add_route` and `include_router`. It claims one when its receiver is a plain name, its path
  is a literal, and the receiver registered routes in the last run's table. Claimed statements
  are left out of rule 3's folding and handled below; anything unclaimed folds as before.
- **Selection for a changed claimed statement.** Route identity is `(file, endpoint qualname)`.
  - **Added, removed, path or method changed:** re-run the tests whose recorded requests match
    the old or new pattern (FULL or PARTIAL, via `starlette.routing.compile_path`). A new
    route's full path is its receiver's runtime prefix, derived from a sibling route of the
    same file and receiver, plus the literal path.
  - **Behavioural keyword arguments** (`response_model`, `status_code`, `dependencies`, …):
    re-run the tests whose matched route it is.
  - **OpenAPI-only keyword arguments** (`tags`, `summary`, `description`, `deprecated`,
    `operation_id`, `include_in_schema`, `responses`, `openapi_extra`) and `name=`: re-run the
    route-table enumerators.
  - **Enumerators:** tests that called `app.openapi()` (so `/openapi.json` and the docs) or
    `url_path_for`/`url_for`, recorded by wrapping those. They depend on every claimed statement.
- **Coarse fallback** (every test using the app):
  - a non-literal path or non-simple receiver;
  - a `Mount` of a non-Starlette app, for that prefix;
  - a pure reorder of two routes (matching is first-wins, and neither route changed);
  - app-level effects: middleware, exception handlers, lifespan, `dependency_overrides` set at
    import. These really do affect every request.

Probe (a 32-test synthetic app, 16 edits, each run before and after): every test whose outcome
changed was predicted, and the adapter predicted 2–7 tests per edit against 32 for the coarse
rule. The edits included an unrelated route, a shadowing route, a new method on an existing path,
path, `response_model`, `status_code`, `dependencies`, tags, removal, rename, an `include_router`
prefix, a body model's `Field` constraint, a query default, and a raising dependency. The
Starlette pieces relied on (`compile_path`, the `scope["endpoint"]` contract, `Router.__call__`)
have been stable since 2019–2023. An unmerged route-index branch keeps them and also sets
`scope["route"]` for plain Starlette. Django (`resolver_match`), Flask (Werkzeug `Rule`) and
Litestar would each need their own adapter; not planned.

### Child processes

This whole section applies only with `[tool.voci] affected_trace_subprocesses = true`. Without it, spawns
just mark tests untrusted. A child's trace is attributed to the collector that spawned it: the
test's, or a fixture's, as with a server subprocess started by a session fixture. Every patch
below is installed at run start and restored at run end.

- **Bootstrap:** the `voci[subprocesses]` extra installs a `.pth` that runs one `os.getenv` and
  does nothing unless `VOCI_AFFECTED_DIR` is set. coverage ships the same thing as
  `a1_coverage.pth`. Measured cost: ~0.4ms per interpreter start when unset, ~4ms when set.
  `VOCI_AFFECTED_DIR` is set in `os.environ` for the whole run. It's a run-wide constant, so
  spawns that inherit the environment (`multiprocessing`'s `env=None`, the forkserver) pick it
  up without a race.
- **Write-through:** the child tracer appends each first-seen code object to
  `$VOCI_AFFECTED_DIR/<pid>` immediately, with an unbuffered `os.write`.
  - A child killed by SIGTERM or SIGKILL, or ending in `os._exit`, has still recorded everything
    it ran, so no exit patching, `atexit` or signal handlers are needed.
  - Children never touch the sqlite store (coverage #1101: sqlite in a signal handler deadlocks).
    The parent merges the files at session end.
- **Per-spawn attribution:**
  - `subprocess.Popen` is patched to pass a *copy* of the env (its own `env=` or `os.environ`)
    with `VOCI_AFFECTED_TEST=<collector token>` added. Mutating the shared `os.environ` instead
    misattributed 11% of 400 concurrent spawns; the copy misattributed none. An `env=` that
    scrubs the environment gets the variables added too.
  - `BaseProcess.start` is patched to put the token on the `Process` object, which travels
    pickled to spawn and forkserver children. A child-side patch of `_bootstrap`, installed by
    `child.py`, reads it back.
    - The env or ContextVar alone isn't enough for forkserver: it is started once and reused,
      and in the probe it attributed three tests' workers to the first one.
    - 3.14 made forkserver the Linux default.
  - `os.register_at_fork(after_in_child=...)` switches a forked child to write-through into its
    own file, keeping the ContextVar it inherited. The `sys.monitoring` registration survives
    `fork` (probed).
- **Traced or untrusted:** a spawn is traced when both hold:
  - argv[0] resolves to `sys.executable`, or to a script whose shebang is that interpreter
    (console scripts in the venv);
  - argv has no `-I`/`-S`, which would skip the `.pth`.

  Everything else makes the spawning collector untrusted: other executables (including `sh -c`,
  `git`, `docker` and foreign interpreters, since any of them can run first-party code out of
  sight), plus `os.system`, `os.posix_spawn` and `os.exec*` called directly. At session end, a
  traced spawn that left no file (the child died before `site` ran) makes the collector
  untrusted.

| Child | Result |
| --- | --- |
| `subprocess` → `sys.executable`, a venv console script | Traced |
| `multiprocessing` spawn / fork / forkserver, `ProcessPoolExecutor` | Traced |
| raw `os.fork()` | Traced (fork hook) |
| Child killed, timed out, or ended in `os._exit` | Traced up to the kill |
| `sh -c`, `git`, other interpreters, `-I`/`-S`, `os.system` | Untrusted |

### `--watch`

- **The parent never imports test or first-party code.** Each iteration is a child
  `python -m voci --affected <argv>`, so a first-party edit is always seen. `--affected` already
  selects failures, new tests and stale tests, so `--lf` is no longer appended. The first
  iteration runs immediately: a full run when there's no store, the affected set otherwise.
- **Watched set,** recomputed from the store (read-only) after each child exits:
  - every `*.py` under the test roots, to catch new tests;
  - every path the store records: first-party `.py`, `data:` and `dir:` entries;
  - the mtime of every first-party package directory, so an added module that could shadow an
    imported name is noticed;
  - `pyproject.toml`, `uv.lock`, and the site-packages directory's own mtime. These are
    environment-key inputs that can change during a session.

  Directories are pruned the way discovery prunes them.
- **Polling:** stat-based every 0.3s, with no new dependency. A run starts only after 150ms with
  no further change, so an editor saving several files, or a `git checkout` rewriting a hundred,
  triggers one run. After switching back to a branch already run, that run selects nothing.
- **A change during a run:** the run finishes, and its mid-run stat guard drops the records of
  files that moved. The next iteration then starts immediately. Ctrl-C goes to the child first,
  which reports its partial results; a second Ctrl-C, or one while idle, exits.
- **Warm fork mode (only if M8 measures it's needed):** the parent pre-imports the third-party
  modules the last run imported, which the store records. It checks that no first-party module
  slipped into `sys.modules` (falling back to spawning if one did), then calls `os.fork()` per
  iteration. It forks before any thread exists, and only on POSIX.

## Failure modes

Gaps are what `verify` and a CI full run are for.

| Failure mode | Answer | Gap |
| --- | --- | --- |
| Function body edit | Def block | — |
| Constant, alias, pydantic/dataclass field, enum member, base, `__slots__` | Statement blocks, name closure (rule 2) | — |
| Decorators, registrations | Effects fold into their target (rule 3) | Coarse for frameworks without an adapter |
| FastAPI/Starlette routes, including 405/422/401 where the handler never runs | Starlette adapter: request patterns, matched route, enumerators | Non-literal paths, opaque mounts, app-level effects → coarse |
| Import-time code (decorator bodies, metaclass) | Collection collector, module-global (rule 3) | Modules first imported inside a test |
| Unresolvable reference, `__getattr__`, `dir(module)` | Whole-module (rule 4) | — |
| Shared-scope fixture | Fixture collector (rule 1) | — |
| `TestClient`, `asyncio.to_thread`, `anyio.to_thread.run_sync`/`Path`, `aiosqlite` | Sound by default — each copies or inherits the caller's context on its own | — |
| A bare `Thread`, `aiofiles`, or a direct `loop.run_in_executor` call | Untrusted by default; opt-in `affected_trace_threads` attributes both | C-started threads → untrusted regardless |
| A known untraceable dependency | `@voci.untrusted(reason)`, always runs, no `verify` prediction to mismatch | — |
| `mock.patch("pkg.mod.name")`, ORM and forward-ref strings | String references (rule 5) | Strings assembled at runtime |
| Dynamic imports, plugin discovery | `import_module` wrapped (rule 6); directory listings are `dir:` deps | Module picked by a runtime value with no import call and no listing |
| Missing module added (cherry-pick fixing an `ImportError`), shadowing, optional dependency installed | `module:` keys (rule 7) | — |
| Process-wide setup at import (`logging`, `warnings`, `load_dotenv`, loop policy) | Session-global effects (rule 3) | Setup done inside a function no test depends on |
| Data files, templates, snapshots | Audit `data:`/`dir:` | C reads with no audit event |
| Subprocesses, `multiprocessing` | Untrusted by default; opt-in `affected_trace_subprocesses` traces same-interpreter children; isolated tests merge their record | Other executables → untrusted |
| Env vars | Recorder; `LANG`/`LC_*`/`TZ` in the key | C `getenv` of other keys |
| Package upgrades | Per-test `module:` keys with requirement closure (rule 7) | Third-party code loaded dynamically outside entry points and requirements |
| Interpreter, config, extensions, plugins | Environment key | — |
| Positional parametrize ids reordered | Test's `def` statement block; data cases via audit | — |
| `skipif`, new tests, failures | Always selected | — |
| A new test added beside only-`SKIP` siblings in an already-recorded file | `select.decide`'s own per-test filter is sound (an unseen test id always decides `RUN`) | `select.candidate_files`'s file-level narrowing can't see it: no dependency key changes when a file gains a sibling def, so the whole file is skipped and the new test is never imported under a real `--affected` run. Needs a key that changes with a file's own top-level bindings; `--affected-verify`/CI don't narrow via `candidate_files` at all, so both still catch it |
| File edited mid-run | Records dropped -- `seeds_for_record`'s own `changed_paths` rule | Nothing populates `changed_paths` yet: `cli.py`'s `_record_test_dependencies` always passes the default `frozenset()`, since detecting a mid-run edit is a stat-watching concern `--watch`'s own M8 rebuild owns, not built. Until then a file edited while `--affected` is running can be stored against slightly stale source |
| Branch switching all day | Several records per test, parse cache by content, per-test package keys, store shared across worktrees | The first visit to each new tree |
| mtime churn, moved cache | Content hashes, relative paths | — |
| Order dependence, time, randomness, network, external DB | — | `verify`, CI full run |

## References

- [research/affected/](../research/affected/README.md): the prototypes behind every "probe" and
  "probed" above, including the name-level analyzer, the Starlette adapter with its 16-edit
  soundness harness, the child tracer, and the subagent reports they came from.

- `oss/pytest-testmon/testmon/`:
  - `process_code.py`: blocks (`:111`); its membership check (`:280`) is replaced here by keyed records.
  - `db.py`: schema (`:340`), environments (`:647`).
  - `testmon_core.py`: sentinel for new tests (`:329`).
  - `configure.py`: no-select reasons.
- testmon issues: #191, #192, #204, #206, #233, #236, #89. #89 is the verify mode users asked
  for and never got.
