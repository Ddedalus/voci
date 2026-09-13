"""Affected-test selection (`plans/affected-tests-plan.md`): re-running only the tests a change
can reach.

`tracer.py` is the M1 "Recording" milestone's first piece: a `sys.monitoring` tool that
classifies running code as first-party or not. `collector.py` is its second: the per-test,
per-fixture and per-file `Collector`s that a running `Tracer` would attribute code to, wired into
`_run.run`, `_di.runtime` and `_collection.collect`'s own bookkeeping already. Later milestones add
what a passing test depends on, where that's stored, and the `--affected`/`--affected-verify`
flags that read it back; none of that exists yet, so nothing in voci starts a `Tracer` today.
"""

from __future__ import annotations
