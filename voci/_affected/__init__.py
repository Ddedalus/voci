"""Affected-test selection (`plans/affected-tests-plan.md`): re-running only the tests a change
can reach.

`tracer.py` is the M1 "Recording" milestone's first piece: a `sys.monitoring` tool that
classifies running code as first-party or not. `collector.py` is its second: the per-test,
per-fixture and per-file `Collector`s that a running `Tracer` would attribute code to, wired into
`_run.run`, `_di.runtime` and `_collection.collect`'s own bookkeeping already, plus the untrusted
marking a collector gets when first-party code runs with none of them current. `threads.py` is the
opt-in `affected_trace_threads` patch that keeps a collector current across a thread or executor
hop, so that marking stays rare. `voci.untrusted(reason)` (`_marks.py`) is the same distrust,
declared by a test itself rather than detected. Later milestones add what a passing test depends
on, where that's stored, and the `--affected`/`--affected-verify` flags that read it back; none of
that exists yet, so nothing in voci starts a `Tracer` today, and `cli._installed_session` doesn't
call `threads.install` yet either.
"""

from __future__ import annotations
