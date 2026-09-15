# `_run/coverage.py` — coverage across the isolated boundary

See [rationale.md](../rationale.md) for the index.

**A subprocess's data is merged into the parent's live measurement, not left on disk.** The
alternative is coverage.py's usual answer for multiple processes: each writes its own
`.coverage.<suffix>` file and the user runs `coverage combine` afterwards. voci declines it
because the number of processes is an implementation detail of a *mark* — adding `@voci.isolated`
to one test would change the command a project's CI has to run, and forgetting to would silently
report that test's lines as unexecuted. `harvest` reads the child's data file the moment the child
exits and updates the parent's `CoverageData` in place, so `coverage run -m voci` leaves exactly
one data file however many isolated tests ran.

**The child gets the parent's whole configuration, not a chosen subset of it.** `subprocess_env`
serializes the live `CoverageConfig` and overrides only where it writes. Picking fields by hand
invites two failures that both surface far from here: a `branch` setting that disagrees across the
boundary produces arc data that cannot merge into line data at all, and a `source`/`omit` the
child doesn't know about credits the report with files the project asked to leave out. What it
does override is `parallel`, forced *on*: the subprocess is not necessarily the only process
measuring under that environment — it inherits into anything the test itself spawns — and one
data file shared between them is two `atexit` saves racing. `harvest` reads back every file whose
name starts with the one it handed out, which is what parallel mode's per-process suffixes leave.

**A measurement problem is a note, never a test failure and never a warning.** Coverage data that
can't be read back, and a coverage.py too old to carry its configuration into a subprocess, go to
`run_suite`'s `note` — the same channel the loop watchdog uses. `warnings.warn` would have been
the obvious choice and is the wrong one: voci's own warning shim honours the user's
`filterwarnings`, so a project running with `["error"]` would have voci's diagnostic raised
inside `run_isolated`, out through the dispatch TaskGroup, and take the whole run down over a
measurement detail. The test genuinely passed; only the accounting of it is missing.
