# `_cache.py` / `_collection/lastfailed.py` — the run cache and `--lf`/`--ff`

See [rationale.md](../rationale.md) for the index.

**The cache orders and narrows a run; it never decides that a selected test can be skipped.** What
is stored is what the last run *found*, not what the suite *contains*, so no reading of it can
disagree with the source tree about which tests exist. `--lf` names a selection ("the last run's
failures") the same way `-k` names one, and every test it keeps is still collected and run from
source. A stale cache therefore costs a `--lf` its precision — a test that no longer exists is
simply not collected — and can never cause a run to report green on a test it silently left out.

**`--lf` prunes the file set before collection, not the record set after it.** Importing the whole
suite and then discarding most of it would give the correct answer at the cost of the entire
saving: importing test modules is what collection actually spends its time on. A test id starts
with its rootdir-relative path, so the set of files that could hold a recorded failure is readable
straight off the ids, with no second index to keep in sync and no staleness question to answer.

**A file that failed to collect is replayed whole.** A broken import contributes no ids, so there
is nothing for `--lf` to match against, and matching against nothing would mean the one run that
had something to say about that file is the one that leaves it out. The cache therefore stores
those paths separately from the failed ids, and every test in such a file counts as a recorded
failure until a run collects it successfully. Where the path is a package `__init__.py` the tree
below it is what gets replayed: discovery never yields an `__init__.py` as a test file, so an
exact match on it matches nothing, and nothing under it was collected either. Narrowing the file
set never narrows what counts as a *test module*, though: `collect` is told the whole discovered
set alongside the slice it is collecting, so a `velox.use(...)` in a file `--lf` left out does not
become a misplaced declaration the moment a collected file imports that module. The same asymmetry
decides when the entry clears — collecting anything beneath a package is what imports it, so a
failure the run did not report again is one the run fixed.

**A run only overwrites what it settled, and absence is an answer.** Merging keeps every
previously-failed id the run produced no result for and every previously-erroring path it did not
collect. Without that, `velox -x --lf` would drop the run's remaining failures the moment
`--maxfail` stopped it, and running one directory would erase the failures found in another — both
of which turn `--lf` into a flag you cannot trust twice in a row. The converse matters just as
much: a recorded id under a file collection read *without error* and did not produce names a test
that has been renamed or deleted, and nothing will ever run it again. Leaving it recorded would
keep the cache permanently non-empty, so every later `--lf` narrows to that file and selects
nothing from it — a wedge only deleting the cache directory recovers from.

**Every entry the cache accepts is one some run can take back out.** That wedge is the failure
mode the whole settling story exists to prevent, and absence-as-an-answer only reaches entries
whose file this run *read*. What escapes it is closed from two directions.

`dead_paths` is the first: a recorded path is dead when the file is gone from disk, or when this
run walked the directory it sits in and discovery produced nothing that reaches it — deleted,
renamed, newly `ignore`d, or no longer matching `test_file_patterns`. "Reaches it" is
`settled_paths` over the discovered set, which makes it the exact complement of settling rather
than a second, looser rule: a package `__init__.py` counts as live only through the unbroken
`__init__.py` chain that would import it, never through a namespace directory sitting under it.
The filesystem half is read against `rootdir` rather than against this run's discovery, and the
discovery half is scoped to the roots actually walked — and to directories those roots *contain*,
not ones they sit inside — so neither `velox one/` nor `velox pkg/sub` concludes anything about
what it only saw part of.

The second is what may go in at all. `error_paths` records a collection error only on a path
`settled_paths` could later answer for; anything else — `_misplaced_declarations` reporting a
`velox.use(...)` in a module velox never collects, named absolutely outside `rootdir` and by a
bare dotted module name with no `__file__` — is reported by every run that imports the module and
not cached. One `settled_paths` call, read both ways round.

Two smaller rules fall out of the same invariant:

* **A file under a broken package** was never read, and carries no error of its own — `collect`
  attributes that one to the `__init__.py`, once, however many files sit beneath it. It is
  excluded from what counts as read, or absence-as-an-answer would drop every failure recorded in
  it. A file that *did* collect tests is read whatever else in it went wrong: one malformed test
  does not make the ids beside it unknowable, and the file is still recorded whole so a `--lf`
  cannot report green while that error stands.
* **`--lf`'s own deselections** are not `-k`'s. A deselected test that never reached expansion
  keeps its recorded `[case]` ids, since the run never built them — true of `-k` and `-m`, and
  false of `--lf`, which deselects a skip it knows the answer for. Settling therefore reads the
  collection result as it was *found*, before `select` narrowed it.

**What is out of a run's *selection* stays recorded, and is said out loud.** A recorded failure
under a root this run was not pointed at is one the run has no answer for, so it survives — but a
`--lf` narrowed to a selection holding none of them would then report `0 tests` and exit `5`,
which reads as a suite that collected nothing rather than as the flag having nothing to do here.
It says so instead. This is the one wedge left deliberately open: the entry is real, and only a
run that reaches it can clear it.

**Which `.velox_cache` a run reads is not the arguments' business.** rootdir fixes the spelling of
every test id, the `sys.path` entry, and the cache directory, so a rootdir derived from the
arguments gives `velox tests/unit` a second cache in a second id namespace. That is worse than
two caches drifting apart: the second one starts *empty*, and an empty cache means "nothing
recorded", which is exactly the state the paragraph above exists to distinguish from "your
failures are all outside this selection". The loud exit `5` silently becomes a green exit `0`.
`_config.resolve` therefore falls back to the nearest `pyproject.toml`'s directory, and to the
search start only when there is none. Nearest, not outermost: in a monorepo the distribution is
what `sys.path` and the ids must be read against, and rooting at the repo above it would stop the
suite's own imports from resolving. The `.git` the walk stops at is deliberately *not* a fallback,
though it still bounds the search — it says nothing about where a suite's imports are rooted, and
it is routinely somewhere a rootdir has no business being, a dotfiles repo at `$HOME` being the
case that decided it.

The cost of climbing at all is that `sys.path` climbs too: a suite whose tests import a helper
module sitting beside them resolved that helper only because `velox tests/unit` used to root
itself there. Anchoring to a `pyproject.toml` alone is what keeps that narrow — a tree with no
`pyproject.toml` anywhere above it, which is what such a suite usually is, does not move.

Rootdir is not the *selection*, though. Climbing to the project root would otherwise widen a bare
`velox` run from inside `tests/unit` into the whole suite, so the built-in default tier
(`cli._default_test_roots`) anchors at the current directory unless a `[tool.velox]` table fixed a
rootdir deliberately — the two used to be the same directory and no longer are.

**Two velox runs sharing one rootdir can still lose each other's failures, but only just.** `save`
is atomic against a torn *read* — a pid-suffixed temporary file replaced into place — but not
against a lost *update*: both runs write a whole payload, so the second to finish wins. What
decides how much that costs is which baseline the loser merged into. Merging into the one loaded
at startup leaves the window open for the entire length of the run, and the damage is not the
harmless kind: a *scoped* run landing after a full one writes a cache that is non-empty and
plausible with the full run's new failures missing from it, which no later `--lf` re-finds.
`update` re-reads at the write instead. A run's findings don't depend on the baseline, so the
freshest one on disk is strictly the better thing to merge into, and the other run's entries —
not being in this run's `settled_*` — are carried forward by the rule that already survives a
`--maxfail` stop. The window shrinks to the gap between that read and the `os.replace`. Still not
locked, and now genuinely worth not locking: the residual cost is a `--lf` that misses a failure
the next run re-finds, against a lock on the exit path of a run that has already reported its
result.

**Every filesystem failure is swallowed.** An unreadable, truncated, hand-edited or
wrong-version cache reads as "nothing recorded", which makes both flags mean "the whole suite in
logical order"; an unwritable tree costs the next `--lf` its ordering. The cache is an
optimization with a correct fallback at every point, and a run's exit code must never depend on
one.

**Records are renumbered after either transform.** `index` is the position of a test in the run
that is about to happen — the reporter's `--durations` tie-break reads it, and a serialized report
carries it — so a `--lf` that removed the tests in front of one, or a `--ff` that moved it, has to
renumber rather than leave the collection-time positions behind.
