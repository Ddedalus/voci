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
`_collection.lastfailed`'s own two-step shape, over the store instead of `LastRun`. `world.py`'s
`build_world` is the first piece of the session-start/end driver M3 still needs: every first-party
file under rootdir (`is_first_party`'s own criterion, not just `discover_files`' `ignore_dirs`),
turned into the `World`/`files` mapping `store.checksums` and `seeds.seeds_for_record` both need.
`_run.run.run_suite`'s `on_test_dependencies` is the other piece already landed: a test's own
`CollectorRecord`, folded together with every `module`/`session`-scope fixture its plan reaches
(`_di.runtime.ScopeStore.collectors_for`), once the whole run -- session teardown included -- is
otherwise done. `driver.py` is the session-start/end driver itself: `prior_selection` builds a
`Selection` against a stored `env_key`, or names `FULL_RUN_NO_MATCHING_ENV` if that environment
has nothing stored yet; `record_test` runs `seeds.seeds_for_record`/`World.closure`/
`store.checksums`/`store_record` for one finished test, skipping a CANCELLED outcome (which never
got to say anything about the code under test) or a record `seeds_for_record` itself dropped.
`environment.py`'s `placeholder_env_key` is a deliberately coarse stand-in for M4's real
environment key -- interpreter implementation/version/platform only, safe to ship ahead of the
real one because being too coarse only costs extra full runs, never an unsound skip. Still to
come: starting a real `Tracer` around the parent's own run (today only `@voci.isolated`'s
subprocess starts one, and its own inner `run_suite` call doesn't pass `on_test_dependencies`
either); and the `--affected`/`--affected-verify` flags themselves, wiring all of it into `cli.py`.
None of that exists yet, so the parent's own run still starts no `Tracer` of its own, and
`cli._installed_session` doesn't call `threads.install` yet either.
"""

from __future__ import annotations
