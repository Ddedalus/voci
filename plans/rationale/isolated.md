# `_run/isolated.py` — the `@velox.isolated` subprocess tier

See [rationale.md](../rationale.md) for the index.

**The subprocess re-collects from source; it is never handed the parent's live objects.** A
`TestRecord`'s `func` and `plan` are ordinary Python objects — closures, DI providers, imported
modules — with no general JSON (or pickle-safe) representation, and forking the parent to clone
them would inherit exactly the process-global state (signal handlers, loop policy, an open thread
pool mid-run) `@velox.isolated` exists to escape. The subprocess is instead handed just a file path
and a target test id, imports that one file fresh, and calls `_collect.collect` on it again —
paying a real re-import cost per isolated test, deliberately, in exchange for a genuinely clean
interpreter rather than a copy of a busy one.

**The wire format is a dict, not a `TestResult`.** `isolated.py` cannot import `run.py`'s
`TestResult`/`Outcome` at module level — `run.py` imports `isolated.py` to dispatch a test there in
the first place, so the reverse import would be circular. `result_to_json`/`_result_from_json`
split the (de)serialization across the boundary instead: the worker (which already imports `run.py`
to actually run the test) builds the dict, and `run.py`'s own `dispatch_one` rebuilds the
`TestResult` from it, with `isolated.py` itself staying a leaf module.

**A child's own `tmp_path` root is nested under the parent's, never passed to it directly.**
`_capture.install`'s explicit-`basetemp` path unconditionally clears whatever directory it's given
before use — correct for a top-level `--basetemp`, catastrophic for an isolated test's subprocess,
which would otherwise wipe the parent's basetemp root out from under every other test still running
concurrently. Each isolated test gets `basetemp_root/isolated/<sanitized-id>` instead: a path
`run_isolated` computes but deliberately never creates itself, so the child's own `install()` call
is the first thing to touch it, finds nothing there, and does a plain `mkdir` with no `rmtree`.

**A crashed or cancelled subprocess still fills `results[index]`.** `run_suite`'s documented
contract is that every dispatched test ends up with a result, whatever went wrong. `run_isolated`
upholds it the same way `_run_one` does for an in-process test: a subprocess that exits non-zero, or
never writes its result file, or is killed by a collateral cancellation from a sibling's
`KeyboardInterrupt`/`SystemExit`, is folded into an `error` result rather than left to leave that
slot empty or the whole run hanging.
