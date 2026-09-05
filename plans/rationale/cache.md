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
exact match on it matches nothing, and nothing under it was collected either. The same asymmetry
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
whose file this run *read*. Three kinds escape it, so each is closed where it arises:

* A deleted or renamed **file** is never handed to collection again, so no import settles it.
  Its entries are settled by the filesystem instead — read off `rootdir`, not off this run's
  discovery, so running one directory still leaves the rest of the suite recorded rather than
  declaring everything it did not look at gone.
* A collection error on a path **discovery does not produce** is never settled either.
  `_misplaced_declarations` reports a `velox.use(...)` in a module velox never collects, named
  absolutely when it lies outside `rootdir` and by a bare dotted module name when it has no
  `__file__`. Such an error is not recorded at all: the rule for what may go in mirrors the rule
  for what comes out, and every run that imports the module finds it again anyway.
* **`--lf`'s own deselections** are not `-k`'s. A deselected test that never reached expansion
  keeps its recorded `[case]` ids, since the run never built them — true of `-k` and `-m`, and
  false of `--lf`, which deselects a skip it knows the answer for. Settling therefore reads the
  collection result as it was *found*, before `select` narrowed it.

**`--lf` deselects skips as well as records.** A collection-time skip is counted as something the
run collected, so leaving a skip-marked sibling in place would let a `--lf` that executed nothing
exit `0` while the recorded failure is still red.

**Every filesystem failure is swallowed.** An unreadable, truncated, hand-edited or
wrong-version cache reads as "nothing recorded", which makes both flags mean "the whole suite in
logical order"; an unwritable tree costs the next `--lf` its ordering. The cache is an
optimization with a correct fallback at every point, and a run's exit code must never depend on
one.

**Records are renumbered after either transform.** `index` is the position of a test in the run
that is about to happen — the reporter's `--durations` tie-break reads it, and a serialized report
carries it — so a `--lf` that removed the tests in front of one, or a `--ff` that moved it, has to
renumber rather than leave the collection-time positions behind.
