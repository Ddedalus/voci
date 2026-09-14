# Running only what changed

Run `voci --affected` to re-run only the tests a change can reach, instead of the whole suite. For
every test that passes, voci records which first-party functions, data files, and environment
variables it touched; the next run skips a test only when none of those have changed since.

```console
$ voci --affected
```

## What triggers a re-run

A passing test is skipped only when every one of its recorded dependencies still matches: the
functions it called, the modules those functions import, the files it read, the environment
variables it read. Editing a comment or reformatting a file changes nothing; editing a docstring
does, since `__doc__` is read at runtime. Editing a function body, changing what a decorator
registers, adding a field to a model a handler depends on, or upgrading a package the test's
imports reach all mark it for a re-run.

A test that failed, errored, timed out, or was skipped runs again regardless of whether its
dependencies changed. So does every new test, and any test voci can't yet vouch for — one that
started a thread or a subprocess it couldn't attribute (see below). Skipping only ever applies to
a test that both passed and kept every dependency unchanged.

## Reading the summary

```console
$ voci --affected
config: pyproject.toml
...
12 tests · 12 passed · 2.1s wall
12 selected · 340 unaffected
```

"Unaffected" is separate from "deselected", which is what `-k`/`-m` produce — the two can appear
in the same run. When nothing in the store lines up with the current tree — the first run, a
stale cache, an interpreter or config change — voci runs everything and says why:

```console
$ voci --affected
config: pyproject.toml
full run: no stored environment key matches
...
```

## Switching branches

voci keeps several dependency records per test, not just the one from the last run, so returning
to a branch it has already run selects nothing to re-run. A lockfile change re-runs only the tests
whose imports reach whatever package changed — a dev-tool bump re-runs nothing; a feature branch
adding a dependency re-runs the tests that import it.

The store lives at `<git-common-dir>/voci/affected.sqlite3` — the same file for every worktree of
a repository — or under `.voci_cache/` outside one. Deleting it is always safe; the next
`--affected` run finds nothing recorded and runs the whole suite.

## Checking the selection before trusting it

Run `voci --affected-verify` to check that `--affected`'s skips are actually safe, instead of
trusting them blind. It runs every test, skipping none of them, but for each one it still
computes what `--affected` would have decided, then compares that prediction against the outcome
the test just produced for real.

When every prediction holds up, the report just adds a count of how many would have been
skipped, alongside the usual totals:

```console
$ voci --affected-verify
config: pyproject.toml
...
352 tests · 12 would have been skipped · 3.4s wall
```

When a test that was predicted to skip — because its stored record says it last passed with these
same dependencies — comes back with a different outcome this time, voci names it as a mismatch
instead of silently counting it:

```console
$ voci --affected-verify
config: pyproject.toml
...
MISMATCH  tests/test_users.py::test_create_user
    predicted a skip (recorded passing) — this run: FAILED
352 tests · 12 would have been skipped · 1 mismatch · 3.4s wall
```

A mismatch means this test's real outcome depends on something voci's tracking doesn't see — the
same handful of things any run of `--affected` can miss: an earlier test leaving behind state,
timing, randomness, a network call, or data in an external database that changed independently of
this checkout. It isn't necessarily a bug in the tracking itself; a genuinely flaky or
order-dependent test will mismatch here on its own, change or no change. Either way, a mismatched
test is one you shouldn't trust `--affected` to skip correctly until you've tracked down why —
`--affected-verify` is what to run periodically, or in CI, to find these before a plain
`--affected` run skips a test that needed to fail.

## Threads and subprocesses

By default voci only observes a test; it never patches `Thread.start`, `subprocess.Popen`, or
`multiprocessing`. A test that starts a thread or process it can't attribute is simply untrusted
and always runs — a wasted re-run, never a missed one. Two `[tool.voci]` keys widen what gets
tracked, each opt-in because it changes something a test could in principle observe:

```toml
[tool.voci]
affected_trace_threads = true
affected_trace_subprocesses = true
```

Set `affected_trace_threads` to patch `Thread.start` for the run's duration, attributing a
thread's work to whichever test or fixture started it. Set `affected_trace_subprocesses` to trace
same-interpreter children instead — `subprocess` calls that launch the venv's own interpreter,
and all three `multiprocessing` start methods; it needs the separate `voci[subprocesses]` extra
installed, so a plain `voci` install patches nothing at interpreter start. Anything else that
spawns a child — another interpreter, a shell command, `git`, `docker` — still just marks the
test untrusted.

## `--watch` runs on `--affected`

Run `voci --watch` to start with whatever `--affected` would select, then keep re-running
failures plus whatever each further change affects:

```console
$ voci --watch
```

[Command line](../reference/cli.md#affected-test-selection) has the full reference for
`--affected`, `--affected-verify`, and `--watch`. [Configuration](config.md) lists
`affected_trace_threads` and `affected_trace_subprocesses` alongside the rest of `[tool.voci]`.
