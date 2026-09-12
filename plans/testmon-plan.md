# Affected-test selection ("testmon")

Re-run only the tests a change can reach. A `sys.monitoring` tracer records, per passing test, the
first-party functions, data files and environment variables it touched. The next run re-runs a
test only when one of those no longer matches the tree.

Status: design settled, not started. The final milestone replaces today's `--watch` with one
built on this. Reference implementation: `oss/pytest-testmon`.

## Work done

- Prototypes. ContextVar attribution is correct under concurrency. A static import graph was
  rejected: the median `oss/fastapi` change selects 56% of the suite, because
  `fastapi/__init__.py` re-exports everything. Line-level tracing was rejected at 1.73x for
  little gain.
- Research pass, 2026-09-12: testmon's source, docs and issues, voci's seams, and probes. Findings
  are folded into Design and Failure modes.

## Work to do

Milestones are in build order. `--affected` stays hidden until M4 lands, because selection
without the non-code dependencies deselects unsafely.

**M1 — Recording** (see Tracer)

- [ ] `voci/_affected/tracer.py`: tool id, callback, first-party classification.
- [ ] Collectors: test (a field on `_capture.TestContext`, already set in
      `run.py:_Session.run_envelope`), fixture (`_di/runtime.py` construct and teardown), and
      collection (`collect._import_module`).
- [ ] Untrusted marking for unattributed first-party code; opt-in `trace_threads`.
- [ ] `@voci.isolated`: the worker ships its record in `result_to_json`, the way coverage's
      `harvest` does.

**M2 — Fingerprints and store** (see Fingerprints, Storage)

- [ ] `voci/_affected/blocks.py`: statement and def blocks, with binds, references and effects,
      from one parse per file.
- [ ] `voci/_affected/resolve.py`: the name closure, effect folding, string index and
      whole-module fallback. Write the soundness cases listed under "Probe results" as voci
      tests before writing selection.
- [ ] `code → block` resolution at session end. Records that touched a file which changed during
      the run are dropped.
- [ ] `voci/_affected/store.py`. Measure its size on voci's suite and on httpx2 first. If it's
      small, a JSON file under `_cache.py`'s discipline replaces sqlite.

**M3 — Selection and CLI** (see Selection)

- [ ] Narrow through `lastfailed.candidate_files`, then filter per test like `lastfailed.select`.
- [ ] `--affected` and `--affected=verify`. Report `N selected · M unaffected` as its own label,
      because `deselected` already means `-k`/`-m`, plus `full run: <reason>`.

**M4 — Non-code dependencies** (see Non-code dependencies, Environment key)

- [ ] Audit hook, `os.environ` recorder, environment key.

**M5 — Child-process tracing, opt-in** (see Child processes)

- [ ] `[tool.voci] trace_subprocesses = true`, off by default. Setting it without the extra
      installed is a config error that names the extra.
- [ ] A `voci-subprocesses` workspace distribution, installed as `voci[subprocesses]`. It holds
      only the `.pth` and the child bootstrap. Check that the `.pth` lands in both a regular
      install and an editable one.
- [ ] Parent side, active only with the key set and only for the duration of a run: patch
      `subprocess.Popen` and `multiprocessing.process.BaseProcess.start`, add the fork hook, sort
      spawns into traced and untrusted, and merge child files at session end.
- [ ] Tests for every row of the child table below, on 3.13 and 3.14, including two concurrent
      tests spawning children at the same time.

**M6 — Measure before recommending it**

- [ ] Overhead on voci's suite and httpx2, alone and with `COVERAGE_CORE=sysmon` coverage.
- [ ] Replay ~200 commits each of `oss/fastapi`, `oss/httpx`, and a real FastAPI *application*
      whose tests run without external services, added as an `oss/` submodule (fastapi's own
      suite tests the library, not an app). Run each under `verify` and record the fraction
      selected, which rule selected each test, and any false greens.

**M7 — Replace `--watch`** (see `--watch`)

- [ ] Delete `voci/_watch.py`, `cli._watch_scope`, the in-process re-invocation wiring in
      `cli.main` (the `wall_start` parameter and the argv filtering), and `tests/test_watch.py`.
- [ ] New parent loop, watched set, and debounce.
- [ ] Measure per-iteration startup on httpx2. Add the warm fork mode only if third-party
      imports dominate.
- [ ] Rewrite the `--watch` sections of `docs/reference/cli.md` and `spec/02-cli-and-config.md`.

## Decisions

Settled:

- Function-level tracing. Each test is compared against its own stored fingerprint, never
  against "what changed since the last run". Branch switches, partial runs, interrupted runs, and
  `-k`/`-m` therefore cost re-runs, never a missed test. testmon has bugs here (#78, #204).
- Only passed tests get a record. Failed, errored, timed-out, skipped, untrusted and new tests
  always run; skips are cheap.
- Comment and whitespace edits invalidate nothing. Docstring edits do, since `__doc__` is read
  at runtime.
- **Exposure:** `--affected` is a flag only, with no `[tool.voci]` key for now.
- **No surprise patching by default.** Anything that changes behaviour user code could observe
  is an opt-in `[tool.voci]` key: `trace_subprocesses` (`Popen`, `multiprocessing`) and
  `trace_threads` (`Thread.start`). Off, the affected tests are simply untrusted.
  - The default path only observes: `sys.monitoring`, an audit hook, a recording `os.environ`
    subclass, and a pass-through wrapper on `importlib.import_module`. Code sees no difference
    in values or types from any of them.
- **`--watch` implies `--affected`.** It starts with the tests that need running, then keeps
  re-running failures plus whatever each change affects. Today's `--watch` is replaced rather
  than fixed. It runs `cli.main` in-process and evicts only test modules, so an edited
  first-party module keeps its old code (probed). It also polls only the test roots. The
  replacement is small, and it depends on the store anyway.
- **Child processes: off by default.** A test that spawns a process is untrusted and always
  runs. Only the audit hook observes the spawn; nothing is patched.
  - Tracing a bounded subset of children is an advanced opt-in (`trace_subprocesses`, M5). It
    patches `Popen` and `multiprocessing`, which users mustn't meet unannounced, and its `.pth`
    ships in an extra, so a default install has nothing that runs at interpreter start.
  - The subset: same-interpreter `subprocess` and all three `multiprocessing` start methods,
    prototyped end to end.
  - Excluded, because they are where coverage.py's subprocess bug history concentrates (#310,
    #1101, #1892, #2137): abrupt-exit patching, signal handlers, and foreign interpreters.

## Design

### Tracer

- **Tool id:** the first free of 3 and 4. If neither is free, report it and don't select.
- **Callback:** a `PY_START` callback classifies `co_filename` once, through a dict cache.
  Non-first-party code returns `DISABLE`. That's sound at any concurrency because the answer
  doesn't depend on which test is running.
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
  - Opt-in `[tool.voci] trace_threads = true` patches `Thread.start` for the duration of a run,
    so the target runs in the creator's context (`Thread(context=)` on 3.14, a wrapped `run` on
    3.13). A server thread started by a session fixture then reports to that fixture.
  - It's opt-in for the same reason as `trace_subprocesses`: threads can observe the change,
    because they see their creator's ContextVars.
- **First-party:** a real file under rootdir, and not under `sys.prefix`, a directory holding
  `pyvenv.cfg` (testmon #206: a venv inside rootdir), or `.voci_cache`. `<string>`, zipimport and
  pyc-only names fail this and are ignored.
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

A record passes while every checksum it holds for a file is still among that file's current
checksums (testmon `process_code.py:280`). Line shifts and edits to unexecuted functions are
therefore invisible, and nothing looks up a qualname at selection time. A cold parse plus hash of
`oss/pytest/src` (39k LOC) takes ~0.5s; after that, only files whose content hash moved are
re-parsed. Don't use `ast.get_source_segment`, which measured 3–5x slower.

### What a passing test depends on

A set of `(path, checksum)` pairs. Tracing supplies the functions that ran; the statements they
reach are resolved statically, per name rather than per file, so a re-export hub costs nothing.

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
   only within that file.
   - A decorator that is a call to a first-party factory also folds onto whatever that factory's
     body mutates, following first-party calls to a bounded depth. The probe's
     `@register("key")` registry was unsound without this.
   - An effect with no first-party target, every import statement (importing runs the target
     module), and collection-traced blocks (decorator bodies, metaclasses) are *module-global*:
     depending on anything in a module pulls them in.
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
7. `data:`, `dir:` and `env:` entries from M4.

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
  never decides a skip. `.py` files are re-parsed only when that hash moved.
- **Selected:** tests whose record is stale, plus those with none. That covers new tests, last
  failures, untrusted tests, and tests in files that had collection errors. A record is pruned
  only when its id is missing from a *fully* collected file.
- **Full run**, with its reason printed:
  - no stored environment key matches;
  - the store is missing or its schema has changed;
  - a non-test `.py` file under rootdir was added or removed (`pkgutil`, `importlib` by name,
    registries);
  - no tool id is free.

  testmon's `configure.py` reasons table is the pattern.

### Non-code dependencies

The audit hook is installed once per process: it can't be removed, and it's a no-op without a
collector. Probes confirmed on 3.13 and 3.14 that it sees `open`, `os.listdir`, `os.scandir`,
`subprocess.Popen` and `sqlite3.connect`.

- **Read-mode `open`** under rootdir → `data:<path>` = content sha. Skipped: `.py` files,
  `__pycache__`, the cache, venvs, and files modified after the session started (outputs the
  suite wrote). Opens during collection land on the file's collector, which covers parametrize
  cases loaded from data.
- **`os.listdir`/`os.scandir`** → `dir:<path>` = hash of the sorted names.
- **`sqlite3.connect`** → treated as a data file.
- **Process spawns** → the spawning collector is untrusted. The hook sees `subprocess.Popen`,
  `_posixsubprocess.fork_exec` (which `multiprocessing` calls directly), `os.posix_spawn`,
  `os.exec`, `os.system` and `os.fork`. With `trace_subprocesses` on, only spawns that Child
  processes can't trace count.

`os.environ` gets swapped to a recording subclass for the run. Its `__getitem__` sees `getenv`,
`get`, `in`, `copy()` and iteration (probed). Each key read becomes `env:<KEY>` = hash of its
value, or "absent". Volatile keys (`PWD`, `SHLVL`, …) only cost re-runs.

### Environment key

A mismatch means a full run. The last 4 keys are kept, so alternating 3.13 and 3.14 wipes
nothing; testmon deletes on every change. The key covers:

- interpreter implementation, full version, ABI flags, and `sys.platform`;
- installed distributions as `(name, version)`, excluding those whose files resolve under
  rootdir. An editable hatch-vcs install, voci included, changes version on every commit, and its
  code is tracked as source anyway;
- the resolved `[tool.voci]`, plus `-W`, `--timeout`, concurrency and assert mode;
- `LANG`, `LC_*` and `TZ`, which C reads without going through `os.environ`;
- hashes of extension modules loaded from under rootdir.

Per-test third-party tracking is out: `DISABLE` leaves no record of which package ran. Any
`uv lock --upgrade` is therefore a full run.

### Storage

- `.voci_cache/affected.sqlite3`, schema version in `user_version`; a mismatch deletes and
  rebuilds it.
- `journal_mode=DELETE`, so it stays one file. CI caches that copied only the main file lost WAL
  data (testmon #233, #236).
- Explicit close, `busy_timeout`, and rootdir-relative paths.
- One write transaction at session end, from the parent only. That's testmon's single-writer
  lesson (#245, #259).
- Tables, after testmon's `db.py:340`: `env(id, key, last_used)`,
  `test(env_id, test_id, outcome, untrusted)`, `test_dep(test_rowid, dep_set_id)`, and
  `dep_set(id, path, checksums, UNIQUE(path, checksums))`, which is shared across tests.

### Child processes

This whole section applies only with `[tool.voci] trace_subprocesses = true`. Without it, spawns
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
  - non-test `.py` files anywhere under rootdir, so an added or removed file triggers the child's
    full-run bail-out;
  - `pyproject.toml`, `uv.lock`, and the site-packages directory's own mtime. These are
    environment-key inputs that can change during a session.

  Directories are pruned the way discovery prunes them.
- **Polling:** stat-based every 0.3s, with no new dependency. A run starts only after 150ms with
  no further change, so an editor saving several files triggers one run.
- **A change during a run:** the run finishes, and its mid-run stat guard drops the records of
  files that moved. The next iteration then starts immediately. Ctrl-C goes to the child first,
  which reports its partial results; a second Ctrl-C, or one while idle, exits.
- **Warm fork mode (only if M7 measures it's needed):** the parent pre-imports the third-party
  modules the last run imported, which the store records. It checks that no first-party module
  slipped into `sys.modules` (falling back to spawning if one did), then calls `os.fork()` per
  iteration. It forks before any thread exists, and only on POSIX.

## Failure modes

Gaps are what `verify` and a CI full run are for.

| Failure mode | Answer | Gap |
| --- | --- | --- |
| Function body edit | Def block | — |
| Constant, alias, pydantic/dataclass field, enum member, base, `__slots__` | Statement blocks, name closure (rule 2) | — |
| Decorators, registrations, `include_router` | Effects fold into their target (rule 3) | FastAPI routes: coarse until the Starlette adapter |
| Import-time code (decorator bodies, metaclass) | Collection collector, module-global (rule 3) | Modules first imported inside a test |
| Unresolvable reference, `__getattr__`, `dir(module)` | Whole-module (rule 4) | — |
| Shared-scope fixture | Fixture collector (rule 1) | — |
| Threads | Untrusted by default; opt-in `trace_threads`; `TestClient` needs neither | C-started threads → untrusted |
| `mock.patch("pkg.mod.name")`, ORM and forward-ref strings | String references (rule 5) | Strings assembled at runtime |
| Dynamic imports | `import_module` wrapped (rule 6); new or removed file → full run | Module picked by a runtime value with no import call |
| Data files, templates, snapshots | Audit `data:`/`dir:` | C reads with no audit event |
| Subprocesses, `multiprocessing` | Untrusted by default; opt-in `trace_subprocesses` traces same-interpreter children; isolated tests merge their record | Other executables → untrusted |
| Env vars | Recorder; `LANG`/`LC_*`/`TZ` in the key | C `getenv` of other keys |
| Packages, interpreter, config, extensions | Environment key | — |
| Positional parametrize ids reordered | Test's `def` statement block; data cases via audit | — |
| `skipif`, new tests, failures | Always selected | — |
| File edited mid-run | Records dropped | — |
| Branch switch, mtime churn, moved cache | Content hashes, per-test comparison, relative paths | — |
| Order dependence, time, randomness, network, external DB | — | `verify`, CI full run |

## References

- `oss/pytest-testmon/testmon/`:
  - `process_code.py`: blocks (`:111`), membership check (`:280`).
  - `db.py`: schema (`:340`), environments (`:647`).
  - `testmon_core.py`: sentinel for new tests (`:329`).
  - `configure.py`: no-select reasons.
- testmon issues: #191, #192, #204, #206, #233, #236, #89. #89 is the verify mode users asked
  for and never got.
