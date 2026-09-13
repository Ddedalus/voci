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
data across the same boundary. `blocks.py` starts M2 "Fingerprints and store": splitting a file's
own parse into the statement and def blocks a `CollectorRecord`'s `(filename, qualname)` pairs
will eventually resolve to. Later milestones add that resolution, where blocks are stored, and the
`--affected`/`--affected-verify` flags that read it back; none of that exists yet, so the parent's
own run still starts no `Tracer` of its own, and `cli._installed_session` doesn't call
`threads.install` yet either.
"""

from __future__ import annotations
