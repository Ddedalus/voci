# Child-process tracing: prior art, prototype, attribution under concurrency

> Subagent report from the 2026-09-12 research pass, kept verbatim. Paths under `/tmp` were rewritten to their copies in `research/testmon/`; the venvs they ran in weren't kept (see `../README.md`).

## 1. Prior art, with bug history

**coverage.py mechanism** (read from `/home/hubert/voci/.venv/lib/python3.14/site-packages/coverage/`):

- The classic path: a `.pth` file whose one line is `import coverage; coverage.process_startup()` gets dropped into site-packages (`pth_file.py:8-16` is the actual template coverage ships). At interpreter startup, `site.py` execs that line. `process_startup()` (`control.py:1443-1506`) checks `COVERAGE_PROCESS_START` (a config-file path) or `COVERAGE_PROCESS_CONFIG` (a serialized config blob, used when re-exec'ing with an inherited config rather than a file), and if set, starts a `Coverage()` instance with `auto_data=True` and a per-process data-file suffix. Getting the `.pth` installed used to be entirely the user's job (issue **coveragepy#367**, "automating the subprocess measurement setup", closed 2016) — a permanent `.pth` bundled in coverage's own wheel only shipped in **7.12.1b1** (2025-11-30, per the changelog), i.e. this was a decade-long rough edge.
- Data gets back via one file per process (`data_suffix=True`), combined afterwards with `coverage combine`, which merges the sqlite data files. Nothing streams live; everything is written at process shutdown via `atexit.register(self._atexit)` (`control.py:654`) or, if `[run] sigterm = true`, from a `SIGTERM` handler (`control.py:655-664, 745-758`).
- Newer, invasive options in `patch.py` (`apply_patches`, `patch.py:22-44`), all added within 7.10.x (2025-07/08 per the changelog — i.e. very recent, ~15 years after the `.pth` trick):
  - `patch = _exit` (`patch.py:47-60`): monkeypatches `os._exit` to call `cov.save()` first — added because `os._exit()` bypasses `atexit` entirely (root-caused in **coveragepy#310**, "Coverage fails with os.fork and os._exit", and **#43**, "Coverage measurement fails on code containing os.exec* methods").
  - `patch = execv` (`patch.py:63-100`): wraps the `execv`/`execve` family to `cov.save()` before the process image is replaced, and — as of 7.10.4 — smuggles the whole serialized config into the new image's env (`new_env["COVERAGE_PROCESS_CONFIG"]`) rather than relying on a file path.
  - `patch = fork` (`patch.py:103-111`): `os.register_at_fork(after_in_child=_after_fork_in_child)`; the child calls `process_startup(force=True)` again (`control.py:1509-1513`) because a `Coverage` instance surviving a bare fork doesn't reliably resume tracing/rotate its data file.
  - `patch = subprocess`: for `subprocess`/`os.system`/exec/spawn families; mostly just makes sure `COVERAGE_PROCESS_CONFIG` is exported.
  - `multiprocessing` gets a separate, non-`patch=` mechanism (`multiproc.py`): it replaces `BaseProcess._bootstrap` itself (`multiproc.py:27-63`) so a `Coverage()` is started/stopped explicitly around the process body, and for `spawn`, sneaks a `Stowaway` object into the pickled preparation data so unpickling it in the child re-applies the monkeypatch (`multiproc.py:66-118`) — this is the direct precedent for the piggyback trick I prototyped in §3.

**Recurring failure classes**, from `gh api search/issues -f q="repo:coveragepy/coveragepy is:issue <term>"` (note: the repo moved from `nedbat/coveragepy` to `coveragepy/coveragepy`; `gh issue list --search` silently returns `[]` against the old slug — use `gh api search/issues`):

| Class | Representative issues |
|---|---|
| Data lost on abrupt exit (`os._exit`, `execv`, SIGKILL) | #310, #43 |
| SIGTERM/signal handling: data not saved, or hangs | #1599 (closed), #1340 (6.3 regression, closed), #2137 (open, 3.14-era: sitecustomize + `COVERAGE_PROCESS_START` hangs `test_threading.py`) |
| Races between subprocess exit and coverage combine/write | #1892 (open, "Race condition leading to hanging tests… multiprocessing…threads"), #1910 ("Spotty coverage of code run through multiprocessing" — explicitly "race condition happening when SIGTERM is received"), #2024 ("patch=subprocess doesn't work when running multiple scripts in parallel") |
| sqlite + signal handlers | #1101 (open): "sqlite acquires a number of locks and mutexes… coverage can run stuff atexit, and atexit can be run via signal handler… not safe to write to a db… will hang forever about 10% of the time" |
| multiprocessing plumbing breaking in installed/site-packages layouts | #1968, #745 |
| `.pth` file mechanics themselves regressing | #2011 ("Test regression around .pth files in 7.10.1") |

The throughline: every one of coverage.py's invasive patches (`_exit`, `execv`, `fork`) exists because the "just install a `.pth` and let atexit handle it" design has a hole for every non-graceful process exit, and the sqlite-write-from-signal-handler deadlock (#1101) is a design smell that's still open today, in a 20-year-old, heavily-resourced tool.

**pytest-cov**: uses its own `.pth`-based subprocess mechanism (its docs' "Subprocess support" page). **#259** is a standing tracking issue ("Ensure subprocess code coverage use case is considered in pth deprecation") opened against a CPython proposal (bpo-33944) to deprecate `.pth` files outright — it was never removed from CPython, but the issue shows the maintainers regard `.pth`-based subprocess support as sitting on infrastructure they don't control and could lose. **#337** ("Proposal: pytest-cov should do less") is the maintainers' own conclusion that pytest-cov's surface area, subprocess handling included, is more than they want to maintain — not a removal, but a stated wish to shed exactly this kind of scope.

**pytest-testmon**: `tarpas/pytest-testmon#16` ("subprocesses support", closed) shows it once wired `COVERAGE_PROCESS_START` through to a testmon-specific rcfile, piggybacking directly on coverage.py's mechanism ("This has been implemented"). `#192` ("multiprocessing does not seem to be supported", **still open**, filed 2022, latest comment 2024: "This is unfortunately still the case with testmon 2.0.9") shows that support silently regressed and was never restored — current `README.md` and source (`gh api repos/tarpas/pytest-testmon/contents`) have zero mentions of subprocess/coverage-process-start. testmon rewrote its tracer to its own AST/block-based scheme (`process_code.py`) rather than coverage.py's line tracer, and the old coverage.py-piggybacked subprocess trick had nothing left to hook into.

## 2. Prototype and startup cost

Built `research/testmon/child-processes/_voci_child_tracer.py`, installed as a `.pth` (`import _voci_child_tracer`) into two throwaway venvs (`research/testmon/child-processes/venv` — 3.14, `research/testmon/child-processes/venv313` — 3.13) plus a bare `venv_noop` (3.14, no `.pth`) for a clean baseline. It's a no-op unless `VOCI_AFFECTED_DIR` is set; when set, it registers a `sys.monitoring` PY_START tool, records `(co_filename, co_qualname)` for files under `VOCI_ROOT`, writes a `<pid>.started` marker immediately and a `<pid>.json` + `<pid>.finished` marker at `atexit`. I later added an `os._exit` monkeypatch (mirroring coverage's `patch=_exit`) after finding it necessary (§3).

Startup cost, 300-sample `subprocess.run([python, "-c", "pass"])`, min/median (min is the honest "pure overhead" estimator on a noisy WSL2 box):

| Condition | min | median |
|---|---|---|
| no `.pth` at all | 11.1 ms | 12.0 ms |
| `.pth` installed, `VOCI_AFFECTED_DIR` unset | 11.5 ms | 12.2 ms |
| `.pth` installed, tracing on | 15.4 ms | 17.1 ms |

Unset overhead is ~0.4 ms (~3.6%, noise-level) — the plan's "should be ~0" claim holds. `-X importtime` confirms the module's own self-time is 0.137 ms unset vs 0.341 ms activated; activated overhead (~4.3ms) is dominated by importing `atexit`/`json`/`time`, not the monitoring registration itself.

**Which children report** (full driver at `research/testmon/child-processes/driver.py`, run twice — once under a foreign interpreter to expose a methodology bug, once correctly under the instrumented venv):

- `subprocess.run([venv_python, script])`, `sh -c "<venv_python> script.py"`, a console-script entry point (built a real `tinypkg` with a `[project.scripts]` entry, installed via `uv pip install -e`), `os.system(...)` — **all report cleanly** (started+finished+json, correct `test_id`).
- `sh -c "python3 script.py"` (bare, resolves to system Python) and a genuinely different venv/interpreter without our `.pth` — **zero markers of any kind**, not even `.started`. Confirmed as *not distinguishable from "no Python ran here"* purely from the child's side.
- `env={"PATH": ...}` (env scrubbed) — same: no crash, no markers, silently untracked.
- `multiprocessing` **spawn**: reports correctly (it's a real `exec`, so `.pth`/env apply normally). Also incidentally surfaced a helper process (semaphore/resource tracker) that reports too, since it's launched with the same interpreter and inherits the env.
- `multiprocessing` **fork**, raw **`os.fork()`**: sys.monitoring's tool registration and the PY_START callback **do survive `os.fork()` natively** (verified in `research/testmon/child-processes/fork_survival_experiment.py` — the forked child kept recording new `(file, qualname)` pairs into its own reset `_seen` set with zero re-registration code). But nothing flushed: multiprocessing's fork-mode `_bootstrap` finally-block calls `os._exit()`, which — like coverage.py's own history (#310, #43) — skips `atexit` entirely, so the child is a silent report failure by default. Patching `os._exit` (mirroring coverage's `patch=_exit`) fixed it in both the raw-fork and mp-fork cases; without that patch, mp-fork produces a `.started` with no `.finished`, ever.
- `multiprocessing` **forkserver**: reports, but see §3 — attribution, not reporting, is the real problem here.
- `ProcessPoolExecutor` (default context): behaves like whichever underlying start method the default context resolves to (spawn/forkserver); same rules apply.
- **SIGTERM, SIGKILL, timeout+kill()**: `.started` present, `.finished`/`.json` absent — exactly the detectable signature the design wants.
- child that itself calls `os._exit(0)`: same signature (started, no finished) *before* the `os._exit` patch; *after* the patch, it reports cleanly, since our patched `os._exit` runs the flush before delegating to the real one.
- Uncaught exception in the child: reports fine (Python still unwinds to a clean interpreter shutdown, `atexit` runs).

## 3. Attribution under concurrency

Two mechanisms, tested against two threads spawning children simultaneously (`research/testmon/child-processes/race_experiment.py`, `research/testmon/child-processes/mp_piggyback_experiment.py`, `research/testmon/child-processes/forkserver_reuse.py`):

**(a) env-var injection for `subprocess`-style spawns.** Mutating the shared `os.environ` right before `Popen(..., env=None)` is racy: 400 concurrent spawns across 2 threads produced **44 misattributions (11.0%)** — a thread-B env write landing in the window between thread-A's env write and thread-A's actual `fork_exec` syscall (which releases the GIL). Building a **private env dict per call** (`env = dict(os.environ); env["VOCI_TEST_ID"] = ...; Popen(..., env=env)`) instead of mutating the global — **0/400 misattributions**. This is the only safe pattern for this family; it also naturally handles `env=None` vs explicit `env=` (always synthesize an explicit dict, never touch the shared `os.environ`).

**(b) multiprocessing bypasses `subprocess.Popen`/`os.posix_spawn` entirely.** `multiprocessing.popen_spawn_posix.Popen._launch` calls `util.spawnv_passfds`, which calls `_posixsubprocess.fork_exec` directly (`multiprocessing/util.py:518-529`) with `env=None` — a Popen-wrapping strategy would never see this call. Its `env=None` does mean it inherits the *live* `os.environ` at the actual syscall (confirmed: `env=None` → `execv`, no snapshot), so mutating `os.environ` right before `.start()` does work for the **spawn** method specifically — but that's still the racy global-mutation pattern from (a), just with `multiprocessing` as the API instead of `subprocess`.

**(c) forkserver reuse is the sharp edge.** The forkserver process is launched once, lazily, on first use, and its own `os.environ`/whatever-context snapshot is frozen at that moment; subsequent `Process.start()` calls only send a pickled target through a pipe — no re-exec, no re-read of env. Reproduced directly: three sequential "tests" using `forkserver` with different `VOCI_TEST_ID` values each time **all three got attributed to the id set for the first one** (`forkserver_reuse.py` output: `first-test`, `first-test`, `first-test`). Worse than a report failure — this is silent misattribution, exactly the false-negative class the plan is trying to avoid. Python 3.14 makes forkserver the Linux default for multiprocessing, so this isn't an edge case if left unfixed.

**The fix that actually works, uniformly, across fork/spawn/forkserver**: patch `multiprocessing.process.BaseProcess.start`/`_bootstrap` (the exact hook coverage.py's own `ProcessWithCoverage` already uses, `multiproc.py:27-63`) to stash the current test id as a plain instance attribute on the `Process` object *before* `start()` is called, and read it back inside `_bootstrap`. For `fork`, it's already in memory (no pickling needed). For `spawn`/`forkserver`, the attribute travels inside the pickled `Process` object itself, which is per-call, not global state — no race, no staleness. Verified in `mp_piggyback_experiment.py`: three forkserver "tests" now correctly get `fs-test-1`, `fs-test-2`, `fs-test-3`; fork and spawn cases equally correct. This also sidesteps the env-mutation race from (a)/(b) since each `Process` instance is thread-private.

**ContextVar across `os.fork()`**: confirmed it survives trivially (fork copies the whole process, including the current `contextvars.Context`) — a direct `cv.get()` in the forked child returned the parent's value with zero plumbing. This matters for a raw `os.fork()` called by test code directly outside multiprocessing: the collector ContextVar the plan already threads through `Thread.start` would carry over for free. It does *not* help forkserver reuse, since the forkserver's own idle process is a separate, long-since-forked process whose Context was fixed at its own launch — same problem as env vars, for the same reason.

**Correlating by pid**: works only for a direct parent→child relationship. It's useless for forkserver workers, whose `ppid` is the forkserver singleton's pid, not the spawning test process's pid (confirmed in the earlier fork_experiment dump — forkserver worker `ppid` pointed at the long-lived forkserver process, not the driver).

## 4. Detecting that a child did not report

The started/finished marker pair is a reliable *sufficient* signal when it fires: any child that reaches Python bytecode execution under our interpreter/venv writes `.started` before doing anything else; only a clean shutdown (now including the `os._exit`-patched path) writes `.finished`. A parent that reaps a pid and finds `.started` without `.finished` can confidently mark the spawning test untrusted — this covers SIGTERM, SIGKILL, timeout+kill, and (pre-patch) `os._exit`/mp-fork.

The gap is children that never get far enough to write `.started` at all: a foreign interpreter/venv, an env with the variable scrubbed, or a process killed before finishing its own startup. These are **indistinguishable from each other, and from "nothing Python-shaped happened,"** from the child's side alone. The existing plan already has the right tool for this: the audit hook. I confirmed `sys.addaudithook` sees `subprocess.Popen`, `_posixsubprocess.fork_exec` (fires for *both* `subprocess.Popen` and `multiprocessing.util.spawnv_passfds`'s direct call — one hook point covers both APIs), and `os.fork` (all three fired in isolated checks). So: the audit hook can synchronously record, at spawn time, "a spawn happened, targeting executable X" and decide immediately whether X is our own instrumented interpreter (e.g. same `sys.prefix`/venv). If it isn't, there's no reason to wait for a marker that will never come — mark untrusted immediately. If it is, wait for the pid to be reaped and require the `.finished` marker.

What can't be told apart even with the audit hook: a non-Python leaf command (`git`, `sh`, `cat`) is invisible in exactly the same way as a foreign Python interpreter that failed to report — both produce a spawn event with no matching markers. The task's own suggested framing holds up under the prototypes: a non-Python binary can't run first-party code itself, so treating it as safe-to-ignore is sound *unless it in turn spawns a Python child of its own* — which we cannot see, because our audit hook only fires in our own process, not inside an opaque child we don't control. There is no way to prove a foreign binary is a leaf; the only sound answer is to keep it conservative (anything that isn't a confirmed instrumented-interpreter spawn stays untrusted), which is a narrowing of today's blanket rule, not a full replacement for it.

## Summary table

| Child kind | Reports? | Attributable? | Detectable if not? |
|---|---|---|---|
| `subprocess.run`/`Popen` (same venv) | Yes | Yes, via private per-call env dict (0% error) | — |
| `sh -c "<venv-python> ..."` (explicit interpreter) | Yes | Yes (same as above) | — |
| console-script entry point (same venv) | Yes | Yes | — |
| `os.system` | Yes | Yes (global env mutation risk unless wrapped) | — |
| `multiprocessing` spawn | Yes | Yes (env works, exec-based) | — |
| `multiprocessing` fork | Yes, *only* after patching `os._exit` | Yes, via ContextVar/fork-inherited state | Yes (started, no finished) pre-patch |
| `multiprocessing` forkserver | Yes | **Only** via `Process.start`/`_bootstrap` piggyback attribute; env/ContextVar silently misattribute to whichever test first launched the server | N/A (misattribution is silent, not a report failure) |
| `ProcessPoolExecutor` | Same as underlying start method | Same as underlying start method | Same |
| raw `os.fork()` (test code) | Yes, if `os._exit` patched or exit is clean | Yes, ContextVar survives fork for free | Yes pre-patch |
| `os._exit()` in child | No, unless patched | N/A | Yes (started, no finished) |
| SIGTERM / SIGKILL / timeout+kill | No | N/A | Yes (started, no finished) |
| Different interpreter/venv, no `.pth` | No | No | **No** at child level; needs audit-hook executable-path check |
| Env scrubbed (`env={"PATH": ...}`) | No | No | Same as above — audit hook, not markers |
| Non-Python child (`sh`, `git`, `cat`) | No (nothing to report) | N/A | Not distinguishable from a failed Python child by markers; audit hook sees the spawn either way; assumed safe unless it spawns Python in turn (unobservable) |

## Effort estimate and honest recommendation

**Fiddly parts, roughly in order of risk:**
1. The `os._exit` monkeypatch is exactly the kind of global, order-of-import-sensitive patch coverage.py itself treats as a last resort (`patch = _exit`, added mid-2025 despite the project existing since ~2010) — any code that caches `os._exit` before our patch installs, or a C extension/`ctypes` call to libc `_exit`/`abort` directly, bypasses it silently. Low-effort to write, but its failure mode is silent, and testing it thoroughly (across the third-party ecosystem, not just our own scripts) is open-ended.
2. The `multiprocessing.process.BaseProcess.start`/`_bootstrap` piggyback is the one genuinely good result here — it's a single, well-precedented monkeypatch (coverage.py has shipped the equivalent for years) and it's the *only* correct fix for forkserver reuse. Still: it needs to interoperate with anything else that also patches `BaseProcess._bootstrap` (coverage.py itself, pytest-cov, pytest-xdist) — multiple monkeypatches stacking on the same method is a known source of "worked until the user also had `--cov` on" bugs.
3. Storage discipline: our tracer must never touch the sqlite store from a forked child or a signal handler — coveragepy#1101 (sqlite deadlock, "not safe in a signal handler") is a direct warning here. The plan's existing "single-writer, from the parent only" design already avoids this as long as child tracers only ever write flat per-pid JSON files, never the sqlite file directly. This needs to be an explicit invariant, not an accident.
4. The audit-hook "is this our instrumented interpreter" check is cheap to build (executable-path/`sys.prefix` comparison) but needs to run fast on every single spawn, in the hot path of a 16-way concurrent test session — worth measuring, not just assuming it's free.
5. `os.posix_spawn`/`os.spawnv*` used directly (not through `subprocess`) weren't prototyped; by analogy with `subprocess.Popen` they should follow the same private-env-dict pattern, but that's an assumption, not a tested result.

**Failure cases that remain even if all of the above is built:**
- Any child using a different interpreter/venv (matrix tests, `uv run --python 3.13` against a bare interpreter, calling out to a system tool that happens to be Python) — untrusted, same as today.
- Non-Python children that in turn spawn Python — unobservable, must stay untrusted or be accepted as a known blind spot.
- A child killed by SIGKILL, or one that segfaults, or one whose C extension calls libc `_exit`/`abort` directly — data lost, but *detectable* (started, no finished) provided our own marker files reliably reach disk before the kill, which is itself not free of races on a sufficiently fast kill.
- `os.fork()` combined with anything exotic in the user's own test code (double-fork/daemonize, `fork()` from a non-main thread) — not tested, and coverage.py's issue history (#1892, #1101) suggests fork+threads+signal-handling combinations are where the deepest bugs live.

**Recommendation, judged by the stated rule:**

Build a **subset**, not the full thing:
- **Build**: same-venv `subprocess`/`os.system`/console-script re-invocation (private per-call env dict — proven 0% misattribution, cheap, no monkeypatching of anything global) and `multiprocessing` in all three start methods (the `BaseProcess.start`/`_bootstrap` piggyback — proven correct including the forkserver-reuse case that would otherwise be a silent correctness bug, and it's a well-trodden monkeypatch coverage.py has carried for years). Pair both with the audit-hook check so non-matching interpreters/scrubbed envs fail fast to "untrusted" instead of waiting on markers that will never arrive.
- **Don't build**: general support for raw `os.fork()` by test code outside multiprocessing, or chasing every abrupt-exit path (`ctypes` to libc, SIGKILL races, forked children that themselves fork again). These are exactly the corner of coverage.py's own bug history (#310, #1101, #1892, #2137) that stayed open or regressed for over a decade in a far more heavily used, far more resourced project. Leave those tests untrusted, as the plan already does.

This subset is a finite, mostly-already-precedented effort (the two hard parts — env-dict-not-global-mutation, and the multiprocessing bootstrap piggyback — are both fully prototyped and working above); the parts that would turn into a bug hellscape (abrupt-exit coverage, arbitrary fork patterns, foreign interpreters) are the parts the recommendation explicitly excludes and leaves on "always untrusted."

**Script paths** (all under `research/testmon/child-processes/`, nothing touched in `/home/hubert/voci`):
- `_voci_child_tracer.py` — the `.pth`-loaded tracer itself
- `driver.py` — the full which-children-report matrix
- `fork_experiment.py`, `fork_survival_experiment.py` — fork/monitoring-survival and `os._exit` patch verification
- `forkserver_reuse.py` — demonstrates forkserver-reuse misattribution
- `race_experiment.py` — env-mutation race vs private-env-dict (11.0% vs 0.0%)
- `mp_piggyback_experiment.py` — the `BaseProcess.start`/`_bootstrap` piggyback fix, verified correct across fork/spawn/forkserver
- `scripts/child_target.py`, `scripts/fp_module.py` — shared test fixtures
- `tinypkg/` — installable package used for the console-script case
- `venv/` (3.14, instrumented), `venv313/` (3.13, instrumented), `venv_noop/` (3.14, no `.pth`, baseline)
