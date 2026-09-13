"""Affected-test selection (`plans/affected-tests-plan.md`): re-running only the tests a change
can reach.

`tracer.py` is the M1 "Recording" milestone's first piece: a `sys.monitoring` tool that
classifies running code as first-party or not. `collector.py` is its second: the per-test,
per-fixture and per-file `Collector`s that a running `Tracer` would attribute code to, wired into
`_run.run`, `_di.runtime` and `_collection.collect`'s own bookkeeping already. `blocks.py` starts
M2 "Fingerprints and store": splitting a file's own parse into the statement and def blocks a
`Collector`'s recorded code objects will eventually resolve to. Later milestones add that
resolution, where blocks are stored, and the `--affected`/`--affected-verify` flags that read it
back; none of that exists yet, so nothing in voci starts a `Tracer` today.
"""

from __future__ import annotations
