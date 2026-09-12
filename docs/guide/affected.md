# Running only what changed

`--affected` re-runs only the tests a change can reach, instead of the whole suite. For every test
that passes, voci records which first-party functions, data files, and environment variables it
touched; the next run skips a test only when none of those have changed since.

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
12 selected · 340 unaffected · 2.1s wall
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

`--affected=verify` runs every test regardless of what the plain flag would have skipped, and
reports which ones that would have been, flagging any whose actual outcome disagreed with what its
stored record predicted. Run it in CI, or whenever a suite has tests that touch things voci can't
see — order dependence, timing, randomness, the network, an external database — to confirm the
selection isn't skipping something it shouldn't.

## Threads and subprocesses

By default voci only observes a test; it never patches `Thread.start`, `subprocess.Popen`, or
`multiprocessing`. A test that starts a thread or process it can't attribute is simply untrusted
and always runs — a wasted re-run, never a missed one. Two `[tool.voci]` keys widen what gets
tracked, each opt-in because it changes something a test could in principle observe:

```toml
[tool.voci]
trace_threads = true
trace_subprocesses = true
```

`trace_threads` patches `Thread.start` for the run's duration so a thread's work is attributed to
whichever test or fixture started it. `trace_subprocesses` traces same-interpreter children —
`subprocess` calls that launch the venv's own interpreter, and all three `multiprocessing` start
methods — and needs the separate `voci[subprocesses]` extra installed, so a plain `voci` install
patches nothing at interpreter start. Anything else that spawns a child — another interpreter, a
shell command, `git`, `docker` — still just marks the test untrusted.

## `--watch` runs on `--affected`

`--watch` starts with whatever `--affected` would select, then keeps re-running failures plus
whatever each further change affects:

```console
$ voci --watch
```

[Command line](../reference/cli.md#affected-test-selection) has the full reference for both
flags, and [Configuration](config.md) lists `trace_threads` and `trace_subprocesses` alongside
the rest of `[tool.voci]`.
