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
- [ ] Context-propagating `threading.Thread.start` while a run is active; untrusted marking.
- [ ] `@voci.isolated`: the worker ships its record in `result_to_json`, the way coverage's
      `harvest` does.

**M2 — Fingerprints and store** (see Fingerprints, Storage)

- [ ] `voci/_affected/blocks.py`: block checksums and import edges from one parse per file.
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

**M5 — Child processes** (see Child processes)

- [ ] `voci/_affected/child.py` and a `voci-affected.pth` in the wheel. Check that the `.pth`
      lands in both a regular install and an editable one (hatchling `force-include`).
- [ ] Parent side: patch `subprocess.Popen` and `multiprocessing.process.BaseProcess.start`, add
      the fork hook, sort spawns into traced and untrusted, and merge child files at session end.
- [ ] Tests for every row of the child table below, on 3.13 and 3.14, including two concurrent
      tests spawning children at the same time.

**M6 — Measure before recommending it**

- [ ] Overhead on voci's suite and httpx2, alone and with `COVERAGE_CORE=sysmon` coverage.
- [ ] Replay ~200 commits each of `oss/fastapi` and `oss/httpx` under `verify`. Record the
      fraction selected, the share of module-block edits, and any false greens.

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
- **Exposure:** the `--affected` flag only, with no `[tool.voci]` key for now.
- **`--watch` implies `--affected`.** It starts with the tests that need running, then keeps
  re-running failures plus whatever each change affects. Today's `--watch` is replaced rather
  than fixed. It runs `cli.main` in-process and evicts only test modules, so an edited
  first-party module keeps its old code (probed). It also polls only the test roots. The
  replacement is small, and it depends on the store anyway.
- **Child processes: trace the bounded subset** and mark everything else untrusted (see Child
  processes). The rule was to build it if finite effort buys better UX, not if it's a bug
  hellscape.
  - Same-interpreter `subprocess` and all three `multiprocessing` start methods were prototyped
    end to end. They cover the common cases: a CLI under test, and worker pools.
  - Deliberately excluded, because they are where coverage.py's subprocess bug history
    concentrates (#310, #1101, #1892, #2137): abrupt-exit patching, signal handlers, and
    foreign interpreters.

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
- **Threads:** neither 3.13 nor 3.14 passes context to new threads (probed). A patched
  `Thread.start` runs the target in the creator's context (`Thread(context=)` on 3.14, a
  wrapped `run` on 3.13). A server thread started by a session fixture then reports to that
  fixture. First-party code that runs with no collector while tests are in flight marks those
  tests untrusted.
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

- **Module block:** module and class-body statements, plus every def's name and decorators.
- **Def blocks:** one per def, covering its arguments, returns, type params and body. A nested
  def's name and decorators stay in the enclosing block; its body gets a block of its own.

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

A set of `(path, checksum)` pairs:

1. Blocks its own collector traced.
2. Blocks traced by each `module`/`session` fixture in `record.plan.steps`, which already
   includes `voci.use(...)` fixtures.
3. The module block, and the collection-traced blocks, of every first-party file in the import
   closure of its test file and of the files in 1–2:
   - Edges come from the same parse, with names resolved through `sys.modules`; no source-root
     config is needed.
   - This covers code that runs only at import: constants read without calling into their module
     (testmon #191, unsolved there), decorator bodies, metaclasses, and dataclass or attrs
     `__init__`s, which trace to `<string>` filenames.
   - It brings the hub problem back for module-block edits only. About 38% of voci's source is
     module block. If M6 shows most commits touch one, narrow this rule to direct imports and
     accept missing constants reached through re-exports.
4. `data:`, `dir:` and `env:` entries from M4.

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
- **Process spawns** that Child processes doesn't trace → the test is untrusted. The hook sees
  `subprocess.Popen`, `_posixsubprocess.fork_exec` (which `multiprocessing` calls directly),
  `os.posix_spawn`, `os.exec`, `os.system` and `os.fork`.

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

A child's trace is attributed to the collector that spawned it: the test's, or a fixture's, as
with a server subprocess started by a session fixture.

- **Bootstrap:** a `.pth` in voci's wheel runs one `os.getenv` and does nothing unless
  `VOCI_AFFECTED_DIR` is set. coverage ships the same thing as `a1_coverage.pth`. Measured cost:
  ~0.4ms per interpreter start when unset, ~4ms when set. `VOCI_AFFECTED_DIR` is set in
  `os.environ` for the whole run. It's a run-wide constant, so spawns that inherit the
  environment (`multiprocessing`'s `env=None`, the forkserver) pick it up without a race.
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
| Constant, import, class attribute, base, decorator | Module block, rule 3 | Hub-coarse |
| Import-time code (decorators, dataclass, metaclass) | Collection collector, rule 3 | Modules first imported inside a test |
| Shared-scope fixture | Fixture collector, rule 2 | — |
| Threads | Propagating `Thread.start` | C-started threads → untrusted |
| `mock.patch` string targets | Target module reached through imports, rule 3 | Never imported anywhere |
| Dynamic imports, registries | New or removed file → full run | Existing module picked by a runtime value |
| Data files, templates, snapshots | Audit `data:`/`dir:` | C reads with no audit event |
| Subprocesses, `multiprocessing` | Same-interpreter children traced; isolated tests merge their record | Other executables → untrusted |
| Env vars | Recorder; `LANG`/`LC_*`/`TZ` in the key | C `getenv` of other keys |
| Packages, interpreter, config, extensions | Environment key | — |
| Positional parametrize ids reordered | Test file's module block; data cases via audit | — |
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
