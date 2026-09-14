"""Affected-test selection (`plans/affected-tests-plan.md`): re-running only the tests a change
can reach.

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

The session-start/end driver M3 still needs is mostly built, just not wired into a real run yet:
`world.py`'s `build_world` turns every first-party file under rootdir (`is_first_party`'s own
criterion, not just `discover_files`' `ignore_dirs`) into the `World`/`files` mapping
`store.checksums` and `seeds.seeds_for_record` both need; `_run.run.run_suite`'s
`on_test_dependencies` gives a test's own `CollectorRecord` folded together with every
`module`/`session`-scope fixture its plan reaches (`_di.runtime.ScopeStore.collectors_for`), once
a run is otherwise done -- `@voci.isolated`'s own subprocess (`_isolated_worker.py`) passes this
too now, over its own fresh `ScopeStore`, so an isolated test's shipped record has the same shape
as an in-process one's; `driver.py`'s `prior_selection`/`record_test` are the actual calls into
`select.py`/`seeds.py`/`store.py` those two feed; `environment.py`'s `placeholder_env_key` is a
deliberately coarse stand-in for M4's real environment key (too coarse only costs extra full runs,
never an unsound skip); `tracing.py`'s `traced` is a `Tracer`'s start/stop lifetime as a context
manager, the same shape `_isolated_worker.py` already used inline, factored out for `cli.py` to
share. Still missing: actually opening `traced` around the *parent's* own run -- nothing calls it
there yet, so none of the above actually runs during a real `voci` invocation -- and the
`--affected`/`--affected-verify` flags themselves, wiring all of it into `cli.py`.
`cli._installed_session` doesn't call `threads.install` yet either.
"""

from __future__ import annotations
