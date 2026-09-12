# Where affected-test selection plugs into voci

> Subagent report from the 2026-09-12 research pass behind `plans/testmon-plan.md`, kept verbatim. Paths under `/tmp` were rewritten to their copies in `research/testmon/`; the venvs they ran in weren't kept (see `../README.md`).

## Affected-test selection ("testmon") — where it plugs into voci

### 1. Test execution envelope: `_run_one` and the ContextVar site

`_run_one` lives at `/home/hubert/voci/voci/_run/run.py:453-565`. It wraps three folded (never-raising) phases: `_run_setup` (line 219), `_run_call` (line 245), `_run_teardown` (line 303), inside one `asyncio.timeout` deadline. But `_run_one` itself is *not* where per-test ambient state is set — it's called from inside `_Session.run_envelope` (`run.py:1054-1154`), which is the real envelope boundary:

```python
# run.py:1120-1142
token = _capture.current_test_context.set(test_context)
try:
    with _warnings.collecting(...) as warned:
        try:
            result, module_keys = await _run_one(record, self.store, timeout=test_timeout, stop=self.stop)
        finally:
            self.worker_slots.release(slot)
        if module_keys:
            self.pending_module_keys.setdefault(record.path, []).extend(module_keys)
        await self.flush_module_scope(record)   # <-- still inside the token
finally:
    _capture.current_test_context.reset(token)
```

This is exactly the site a testmon ContextVar should ride — `current_test_context` (`_builtins/capture.py:234-236`) already does the identical job for stdout/stderr/log attribution, and its docstring at `run.py:1099-1106` says so explicitly: "wrapping the call is enough to attribute everything it does, transitively, to this test." Setting it here means setup, call, teardown, *and* an end-of-module fixture flush are all covered for `scope="function"`/`"call"` fixtures.

Two gaps the plan should know about, both already visible in existing behavior:
- **Module-scope teardown** (`flush_module_scope`, `run.py:1022-1053`) is attributed to whichever test happens to be the *last* to finish its module under concurrent dispatch — not necessarily the test that built or exercised the fixture. This is called out in a comment at `run.py:1135-1136`.
- **Session-scope teardown** runs in `store.aclose()` (`run.py:1345`), *after* `session.run_all()` returns, with **no `TestContext` set at all** — it is truly unattributed to any test.

Both gaps are why the plan's "static fixture union" is necessary rather than optional: `Fixture._func` (`_di/fixtures.py:598`, exposed via the `.func` property at line 618) is one callable whose body covers *both* setup and teardown (a generator function, run to its `yield` and resumed past it — see `_construct_gen`/`_construct_asyncgen` in `_di/runtime.py:384-429`). Hashing that one function's source and adding it via `record.plan.steps` (`_di/fixtures.py:824`, each a `PlanStep` carrying `.fixture: Fixture[Any]` at line 792) means the whole fixture body is captured statically for every test whose plan reaches it — sidestepping the runtime-attribution ambiguity for module/session scope entirely, since nothing needs to be traced there in the first place.

`voci.use(...)` declarations are folded into the same mechanism for free: `plan_for`'s `implicit=` argument (`_di/fixtures.py:836-923`) walks package-declared fixtures into `steps` exactly like a `Depends()`-reached one, so a static per-test closure over `record.plan.steps` already includes the enclosing `__init__.py` chain (`_collection/requires.py:31-107`, `package_inits` at line 57) with no extra walk needed at selection time.

### 2. Concurrency: `ContextPropagatingExecutor`, and what else runs outside a test's context

Confirmed as claimed. `ContextPropagatingExecutor.submit` (`_builtins/capture.py:356-378`):

```python
def submit(self, fn, /, *args, **kwargs):
    ctx = contextvars.copy_context()
    return super().submit(cast(Callable[..., Any], ctx.run), fn, *args, **kwargs)
```

It is installed as the loop's default executor at `run.py:1157` (`asyncio.get_running_loop().set_default_executor(self.executor)`), and `_run_call` dispatches a sync test's body through it via `run_in_executor(None, ...)` at `run.py:266-271`. Any `asyncio.to_thread(...)` call inside a test or fixture body also lands on this same pool, so it's covered too.

Other places user code runs, and their attribution status:
- **Module-scope fixture teardown** (`flush_module_scope`) — attributed to whichever test is the module's last finisher, not necessarily a semantically meaningful one (see item 1).
- **Session-scope fixture teardown** (`store.aclose()`, `run.py:1345`) — runs with no `TestContext` at all, on the main loop thread, after all tests finished.
- **`LoopWatchdog`**'s background thread (`_run/safety.py:183`, `voci-loop-watchdog`) — never runs user code, only inspects `sys._current_frames()` (`safety.py:107`); not a concern.
- **`@voci.isolated`** tests run in a wholly separate process (`_run/isolated.py`), sharing no `ContextVar` state with the parent at all — needs its own independent tracer instance (see item 5).
- No other bare `threading.Thread` runs test/fixture code anywhere in the codebase (grepped `_run/`, `_builtins/`).

### 3. Existing `sys.monitoring`/`settrace` use — none

`grep -rn "sys.monitoring\|settrace\|COVERAGE_CORE"` over `voci/` (excluding the vendored assertion rewriter) returns nothing. voci never touches low-level tracing itself. `voci/_run/coverage.py` delegates entirely to the third-party `coverage` package via its public API (`coverage.Coverage.current()`, `.process_startup()`, `.get_data()`, `CoverageData`) — whatever tracing backend coverage.py picked (legacy `settrace`, or `sys.monitoring` tool id 1 under `COVERAGE_CORE=sysmon`) is invisible to and untouched by voci. Nothing anywhere calls `sys.monitoring.restart_events()` — that would be new machinery the DISABLE-based measurement mode requires.

Coexistence: a voci tool at id 3 or 4 registering `PY_START` should not *correctness*-conflict with coverage's tool id 1 doing the same — `sys.monitoring` tools are independent, each with its own event mask and its own `DISABLE` semantics per (tool, code, event). But this is unverified against real coverage.py behavior and the plan's own overhead numbers (2.56x on call-heavy code) were measured with voci's tracer alone — worth re-measuring with `coverage run` (sysmon core) simultaneously active, since voci already supports running under coverage and merges data across the `@voci.isolated` boundary (item 5), so "testmon selection + coverage measurement, together" is a realistic combination, not an edge case.

### 4. Assertion rewriting — `co_filename`/`co_qualname`/lines, and rewrite scope

Hook: `_assertions/rewrite.py:301-356` (`install`), consulted explicitly at `_collection/collect.py:806-844` (`_import_module`) since `spec_from_file_location` alone never gives `sys.meta_path` a chance to run. The actual compile step, in vendored code:

```python
# _assertions/_vendor/rewrite.py:393-400
strfn = str(fn)
tree = ast.parse(source, filename=strfn)
rewrite_asserts(tree, source, strfn, config)
co = compile(tree, strfn, "exec", dont_inherit=True)
```

`co_filename` is the real file's own path string — unaffected by rewriting. The rewriter only replaces `assert` statements in place, using `ast.copy_location` throughout (e.g. `_vendor/rewrite.py:1094,1125,1142,1186`) so synthesized temp-assignment nodes inherit the *original* statement's line, and it never moves a function's own `def`/decorator line (`_vendor/rewrite.py:759-776` only handles which line an *inserted import at function top* gets, not the function's own boundary). `co_qualname` is likewise untouched — the rewriter renames nothing. Practically: `real_function(func).__code__.co_firstlineno` (read at `_collection/collect.py:698,850` for definition-order sorting) is the same whether or not the file was rewritten. This confirms fingerprinting by `(file, qualname)` over raw source text is stable and gains nothing from the rewrite step being version-sensitive.

**Is first-party (non-test) source ever rewritten? No.** `install(roots, ...)` is called from `cli.py:1040` with `roots = prepared.roots` — the *test* roots (`tests/`, or whatever `PATHS`/`testpaths` resolve to), never the application package. First-party application code (e.g. `voci/`'s own package, or a project's `myapp/`) is imported through the ordinary `sys.path`-based import machinery (`rootdir` inserted once at `cli.py:1020-1023`) and is **never** passed through `_import_module`'s rewrite-hook path at all, and stays normally resident in `sys.modules` for the process's life (unlike test modules — see item 10). So `(file, qualname)` fingerprints for first-party source are simply the original file's own stable identity; the rewriter is only ever a concern for files under the test roots (test files and same-directory helpers), and even there it changes nothing about qualname/filename/def-line.

### 5. `@voci.isolated` — the subprocess boundary

Parent side: `_run/isolated.py:98-203` (`run_isolated`) writes a JSON config, spawns `python -m voci._run._isolated_worker`, and reads back a JSON result via `result_to_json`'s shape (`isolated.py:58-79`). Worker side: `_run/_isolated_worker.py:28-104` re-collects the *one* file naming the target id, runs it through the same `run_suite` (`concurrency=1`, `already_isolated=True`), and writes `result_to_json(results[0])` to `RESULT_PATH`.

Coverage rides exactly this seam already: `coverage.subprocess_env` (`_run/coverage.py:48-69`) hands the child a `COVERAGE_PROCESS_CONFIG` env var pointing it at its own data file; `coverage.harvest` (`coverage.py:88-118`) merges it back into the parent's live measurement once `proc.communicate()` returns (`isolated.py:172,190`). A per-test dependency map would ride the identical two-part pattern: the worker installs its own independent `sys.monitoring` tool (a fresh interpreter, so no tool-id or ContextVar sharing with the parent is possible or needed), and serializes its `{(file, qualname)}` set into the same result dict `result_to_json` already builds, for the parent to merge — no new IPC channel needed, only a new field in an existing JSON shape.

### 6. Discovery narrowing — `lastfailed.candidate_files`, and per-test deselection

`candidate_files` (`_collection/lastfailed.py:86-90`) narrows at **whole-file** granularity only:

```python
def candidate_files(files, last_run, *, rootdir):
    recorded = Recorded.of(last_run)
    ...
    return [path for path in files if recorded.could_hold_one(display_path(path, resolved_rootdir))]
```

Wired from `cli._collect_and_narrow` (`cli.py:1254-1299`), gated on `session.replay_last_failed`. **A finer, already-existing seam exists at per-test granularity**: `_lastfailed.select` (`lastfailed.py:93-113`) filters an already-collected `CollectionResult.records`/`.skipped` down to only the recorded ids, moving everything else into `collected.deselected` (a list of ids) — this is precisely the mechanism Option B needs for "collect the file (because one of its tests could be affected) but only run some of its tests": import the file via `candidate_files`-style narrowing, then apply a `select`-shaped filter keyed on the trace map instead of `LastRun`. The report side already knows how to show this: `deselected` flows straight into `Reporter.finish`/`_print_counts` (item 8) with no new report surface needed.

Test ids: `f"{relpath}::{name}"`, with `name = f"{candidate.name}[{case_id}]"` when parametrized (`_collection/collect.py:673-707`). Stable across runs *if* case content is stable, but `case_value_id` (`_di/fixtures.py:478-499`) falls back to positional `f"{argname}{index}"` for any value that isn't `bool|str|int|None|Enum` — so reordering (not just changing) a parametrize list of non-primitive values silently changes ids with no content change at all, which would corrupt a persistent id-keyed map. Worth flagging as its own bail-out trigger, not just "dynamic parametrization."

### 7. Cache infra — `_cache.py`, `index.py`; no sqlite anywhere

`_cache.py`: `_SCHEMA_VERSION = 1` (`_cache.py:38`, mismatched files discarded whole at line 81), atomic replace via per-pid temp file + `os.replace` (`_cache.py:96-106`, mirrored in `_collection/index.py:128-135`), and the "re-read freshest on disk, merge into that" discipline in `_cache.update` (`_cache.py:109-151`) to avoid a lost-update race between concurrent invocations sharing a rootdir. `index.py` fingerprints by `(mtime_ns, size)` (`_collection/index.py:264-271`) and states its own invariant outright at lines 12-15: *"Nothing here is proactively invalidated... leaving a stale one in place is always safe and only ever wasteful."* — the exact rule the plan says Option B has to break.

**No sqlite usage exists anywhere in voci today** (grepped; only `json`). A per-test function map would be the first thing in the codebase reaching for it, and would need its own schema-versioning/atomic-replace/merge story analogous to the two files above — none of that infrastructure currently generalizes past "one JSON blob per file, whole-payload replace."

### 8. CLI/config wiring — `--lf`, `--watch`, and the report line

`--lf`/`--ff`/`--watch` are defined at `cli.py:88-116`. `--lf`/`--ff` are applied in `_RunSession.replay_last_failed`/`replay_failed_first` (`cli.py:1094-1103`) and consumed in `_collect_and_narrow` (`cli.py:1254-1299`). `--watch` lives in `voci/_watch.py`: `run()` (`_watch.py:47-83`) calls `run_once` (= `cli.main` itself, called again **in-process**, per its own module docstring) once up front, then polls `_scan`/`_wait_for_change` (`_watch.py:86-117`) at `_POLL_INTERVAL = 0.3`s over `discover_files(roots, patterns=("*.py",), ...)`, appending `--lf` to subsequent invocations unless the original invocation already picked `--lf`/`--ff` (`_watch.py:69`, `cli.py:1602`).

Note: `_wait_for_change` already computes a before/after `(mtime_ns, size)` snapshot diff but only returns the new snapshot (`_watch.py:103-116`) — it discards *which* paths changed. Feeding a testmon selector the changed-file set from `--watch` (the plan's own "where it pays off" case) would need this function to also return the changed-path set, a small, localized change.

Report line: `Reporter.finish` (`_report/terminal.py:286-343`) → `_print_counts` (`_report/terminal.py:385-466`) already prints a `deselected` count on the totals line, fed from `collected.deselected` at `cli.py:1494` inside `_report_run`. An "N unaffected, skipped" line is a direct reuse of this existing field — no new report surface needed, matching the reuse of `_lastfailed.select`'s pattern in item 6.

### 9. `_mocking.patching_of`

`_mocking.py:55-82`. Returns `Patching(targets: tuple[str,...], positional_args: int, keyword_args: frozenset[str])`, already computed at collection time for every test (`_collection/collect.py:526`, stored as `TestRecord.patches` at line 704) — purely to decide `solo_for_patching` (`_run/run.py:568-574`). Its `targets` are decorator-form `mock.patch(...)`/`mock.patch.dict(...)` string targets only, read off `func.patchings`/closures on the wrapper object (`_mocking.py:85-117`); the module's own docstring is explicit that `with mock.patch(...)` *inside a test body* "leaves nothing to find" and is only caught at runtime by the process-global guard (`_mocking.py:190-253`). Useful for a testmon design exactly as the plan's own "Failure modes" section already anticipates (unioning statically-known string targets into a dependency closure) — but only for the decorator form, and this data is already computed at zero extra cost since `patching_of` runs for every collected test regardless.

### 10. Other things that would bite

- **Module caching under `--watch`/repeated runs — the plan's worry doesn't apply.** `_import_module` (`_collection/collect.py:806-844`) pops the module from `sys.modules` immediately after `exec_module` succeeds (lines 838-843), specifically so a later, unrelated import of the same dotted name never resolves to a stale entry. This means **every** `collect()` call — including every `--watch` iteration, since those are `cli.main` called again in-process — does a fully fresh `exec_module` of every test file, re-running its module-level code from scratch. There is no cross-run staleness risk for the tracer to worry about; the concern in the plan ("if not reloaded, module-level code isn't re-executed") is already false for voci by construction. (Cost, not correctness: this means the cold-import price is paid every run regardless of testmon, already a known property, not new.)
- First-party application code, by contrast, *is* left in `sys.modules` for the life of the process (ordinary Python import semantics, since it's never routed through `_import_module`) — so an in-process re-run (`--watch`) will **not** re-execute first-party module-level code on a source edit unless the process restarts. This is the flip side worth calling out explicitly: test-side reimport is fresh every time, first-party-side reimport is not, under `--watch`.
- **No plugin/entry-point system exists** in voci at all (grepped `entry_points`/`importlib.metadata`/`plugin` — zero hits). One whole category of pytest-analogous risk (a plugin registering fixtures or hooks dynamically, outside static analysis) simply doesn't apply here.
- **Rootdir is already the "first-party" anchor the plan needs**, with zero new config: `Config.rootdir` (`_config.py:47-95`, resolved by `_config.resolve`) is exactly the boundary already used to decide the one `sys.path` insertion (`cli.py:1020-1023`). Option B's claim in the plan ("first-party is decidable from `co_filename` directly, under the rootdir") is fully supported today with no new surface.
- **`voci.use(...)` and package `__init__.py`** are covered for free via `plan_for`'s `implicit=` argument threading declared fixtures into `record.plan.steps` (item 1) — no separate `package_inits` walk needed at selection time, only at collection time (which already happens).
- A pre-existing, code-object-based test-attribution mechanism already lives in `_run/safety.py` (`code_index`, lines 116-133; `_owning_test`, lines 102-113), used by `LoopWatchdog` to blame a stuck thread on a test. It explicitly can't disambiguate a `@voci.parametrize`d function's shared code object across cases (`safety.py:104-106,116-120`) — this is a good cross-check that the **ContextVar**-based design (not a code-object/frame-based one) is the right call for Option B, since it sidesteps that exact ambiguity by attributing via the running test's task context rather than by which code object a frame belongs to.

## Obstacles / surprises the plan doesn't account for

1. **Session-scope fixture teardown has no `TestContext` at all** (`run.py:1345`, `store.aclose()` runs after `run_all()` returns). This isn't a new risk beyond what the plan already flags for session-scope *construction*, but it's worth being explicit: a testmon design relying on runtime tracing for fixture attribution would get *nothing* for teardown, of any scope, at end-of-run. The plan's static-fixture-union approach already avoids depending on this, since `Fixture._func`'s single hashed body already covers setup+teardown together — good, but worth confirming the plan's phrasing doesn't accidentally suggest tracing could ever "close the hole" for teardown; it structurally can't, for any fixture scope, at end-of-run.
2. **Module-scope teardown is attributed to an incidental test** ("whichever test happens to be the module's last to finish" — `run.py:1135-1136`), not a semantically stable one. Not a correctness problem for the plan's static-union design, but worth naming so nobody is tempted to use tracing as a cross-check for module-scope attribution.
3. **Coverage.py interaction is unverified, not just theoretically fine.** Both testmon's PY_START tool and coverage.py's own sysmon-mode tool would be live simultaneously in the realistic case (voci already supports and merges coverage across the `@voci.isolated` boundary), and the plan's 2.56x/1.03x overhead numbers were measured without coverage active. Combined overhead should be measured before committing to "full concurrency without DISABLE" as the default trade.
4. **Test-id stability under parametrize reordering** (`_di/fixtures.py:478-499`, `case_value_id`'s positional fallback) is a concrete mechanism for a persistent id-keyed map to silently mismatch even with *no* content change — narrower and more mechanical than the plan's general "parametrization over dynamic data" bullet, and worth its own line in the bail-out set (or its own note that non-primitive-valued `params=`/`@voci.parametrize` axes need id stability audited before trusting a persisted map against them).
5. **`--watch`'s snapshot diff is currently thrown away** (`_watch.py:103-116` returns only the new snapshot). The plan's strongest selling point ("`--watch` is the strongest case... feed changed-file sets directly") needs this small change first; it isn't automatic from what's there today.
6. **Assertion rewriting turned out to be a non-issue for the chosen granularity**, more thoroughly than the plan implies — `co_filename`, `co_qualname`, and a function's own `co_firstlineno` are all unaffected by the rewrite (confirmed at the `ast.copy_location`/`compile(tree, strfn, ...)` level), and only test-root files are ever rewritten at all, never first-party source. This *strengthens* the plan's qualname/source-text fingerprint choice, but the plan's own text ("assertion rewriting changes both [line numbers and code objects]") slightly overstates the mechanism — code objects change on every reparse regardless of rewriting (true of any `.py` recompiled), and rewritten line numbers only ever collapse *within* one already-rewritten assert's own span, never shift an unrelated function's definition line. Worth a one-line correction in the plan so "assertion rewriting" isn't cited as the reason to key by qualname over lines — general source edits are the real reason (also already stated correctly elsewhere in the plan's own text).
