"""Affected-test selection: re-running only the tests a change can reach.

`tracer.py` is the M1 "Recording" milestone's first piece: a `sys.monitoring` tool that
classifies running code as first-party or not. `collector.py` is its second: the per-test,
per-fixture and per-file `Collector`s that a running `Tracer` would attribute code to, wired into
`_run.run`, `_di.runtime` and `_collection.collect`'s own bookkeeping already, plus the untrusted
marking a collector gets when first-party code runs with none of them current. `threads.py` is the
opt-in `affected_trace_threads` patch that keeps a collector current across a thread or executor
hop, so that marking stays rare. `voci.untrusted(reason)` (`_marks.py`) is the same distrust,
declared by a test itself rather than detected. `_run/_isolated_worker.py` starts M1's first real
`Tracer` -- one per `@voci.isolated` subprocess, a fresh interpreter that shares no tool id or
ContextVar with the parent -- and ships what it saw back as a `CollectorRecord`
(`_run/isolated.py`'s `collector` key), the way `coverage.py`'s own `harvest` carries measurement
data across the same boundary. M2 "Fingerprints and store" builds on that: `blocks.py` splits a
file's own parse into statement and def blocks; `resolve.py`'s `World` resolves a block's static
references into dependency keys (the name closure, effect folding, string index and whole-module
fallback) and, via `resolve_code`, maps a `CollectorRecord`'s bare `(filename, qualname)` pairs
back to those keys in the first place; `seeds.py` drives that per record, dropping one outright if
it names a file that changed mid-run; `store.py` is where the resulting seeds are checksummed
(`Fingerprints`, `module_checksum`) and stored (`store_record`, in a sqlite3 file shared across a
repository's worktrees) and read back (`load_records`). M3 "Selection and CLI" builds on that:
`select.py`'s `decide` answers, per test, whether its most recent record still matches the tree's
current checksums, and `Selection` holds every test's answer for `candidate_files` (which files
are worth importing) and `select` (which of their tests actually run) to share --
`_collection.lastfailed`'s own two-step shape, over the store instead of `LastRun`.

`world.py`'s `build_world` turns every first-party file under rootdir (`is_first_party`'s own
criterion, not just `discover_files`' `ignore_dirs`) into the `World`/`files` mapping
`store.checksums` and `seeds.seeds_for_record` both need; `_run.run.run_suite`'s
`on_test_dependencies` gives a test's own `CollectorRecord` folded together with every
`module`/`session`-scope fixture its plan reaches (`_di.runtime.ScopeStore.collectors_for`), once
a run is otherwise done -- `@voci.isolated`'s own subprocess (`_isolated_worker.py`) passes this
too, over its own fresh `ScopeStore`, so an isolated test's shipped record has the same shape as
an in-process one's; `driver.py`'s `prior_selection`/`record_test`/`verify_prediction` are the
calls into `select.py`/`seeds.py`/`store.py` those feed; `environment.py`'s `placeholder_env_key`
is a deliberately coarse stand-in for M4's real environment key (too coarse costs extra full runs,
never an unsound skip); `tracing.py`'s `traced` is a `Tracer`'s start/stop lifetime as a context
manager. `cli.py` now wires all of it into `main`, behind `--affected` and its sibling
`--affected-verify`, both `argparse.SUPPRESS`-hidden until M4: `_prepare_affected` opens the
store/`World`/`Selection` before collection (either flag), `traced` wraps collection and the run,
`_collect_and_narrow` applies `candidate_files`/`select` the same way it already does for `--lf`
-- but only for plain `--affected` (`session.verify` gates it off, since `--affected-verify` runs
everything) -- and `_execute_suite`'s `on_test_dependencies` (bound through
`_record_test_dependencies`) calls `record_test` per finished test either way.
`--affected-verify` additionally checks `verify_prediction` against each real outcome as it's
known and reports any mismatch (`_report_verify`). A `Tracer` that can't claim a tool id disables
narrowing and recording/verifying alike for that run rather than risk storing a vacuous,
always-matching dependency set. The `N selected · M unaffected` summary line for plain `--affected`
(`_report_affected_summary`, `Selection.unaffected_count_among`) is in too. `cli._installed_session`
doesn't call `threads.install` yet either.

M4 "Non-code dependencies" builds on that: `audit.py`'s process-wide `sys.addaudithook` callback
turns a read-mode `open`, `os.listdir`/`os.scandir`, and `sqlite3.connect` into `data:`/`dir:`
dependencies (`resolve.py`'s new `DataKey`/`DirKey`) on whichever collector is current, and marks
a process spawn's own collector untrusted -- except `@voci.isolated`'s own spawn (`_run/
isolated.py`), exempted via `audit.exempt_own_spawn` since its dependencies already travel back as
a `CollectorRecord`. `environ.py` patches `type(os.environ).__getitem__` the same unconditional
way, turning each env var read into an `EnvKey`. `seeds.non_code_keys_for` turns a finished
`CollectorRecord`'s `data_paths`/`dir_paths`/`env_names` into those keys directly -- no resolution
needed, unlike a traced `(filename, qualname)` pair -- and `driver.record_test` folds them in
alongside `World.closure`'s own output. `store.py` gained the three keys' checksums (`data_
checksum`/`dir_checksum`/`env_checksum`) and the storage-key encoding for all three. `environment.
env_key` replaces the M3 placeholder with a real one: interpreter/platform, the resolved
`[tool.voci]` plus the three-tier-resolved concurrency/timeout/filterwarnings and `assert_mode`,
`LANG`/`LC_*`/`TZ`, and first-party compiled-extension hashes -- everything but the entry-points
piece, deliberately deferred (see that module's own docstring for why). `tracing.traced` now
installs the audit hook and `os.environ` recorder alongside the `Tracer`, so both the parent run
and `@voci.isolated`'s own subprocess get all of M1-M4's recording from one context manager.
Still missing: unhiding the flags (M5's Starlette/FastAPI adapter and M6's child-process tracing
remain their own milestones, not gates on unhiding), and the entry-points piece of `environment.
env_key` (`environment.py`'s own docstring).
"""

from __future__ import annotations
