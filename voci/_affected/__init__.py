"""Affected-test selection (`plans/affected-tests-plan.md`): re-running only the tests a change
can reach.

`tracer.py` is the M1 "Recording" milestone's first piece: a `sys.monitoring` tool that
classifies running code as first-party or not. Later milestones add what a passing test depends
on, where that's stored, and the `--affected`/`--affected-verify` flags that read it back; none of
that exists yet, so nothing in voci starts a `Tracer` today.
"""

from __future__ import annotations
