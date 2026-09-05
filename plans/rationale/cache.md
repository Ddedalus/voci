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
failure until a run collects it successfully.

**A run only overwrites what it settled.** Merging keeps every previously-failed id the run
produced no result for and every previously-erroring path it did not collect. Without that,
`velox -x --lf` would drop the run's remaining failures the moment `--maxfail` stopped it, and
running one directory would erase the failures found in another — both of which turn `--lf` into a
flag you cannot trust twice in a row.

**Every filesystem failure is swallowed.** An unreadable, truncated, hand-edited or
wrong-version cache reads as "nothing recorded", which makes both flags mean "the whole suite in
logical order"; an unwritable tree costs the next `--lf` its ordering. The cache is an
optimization with a correct fallback at every point, and a run's exit code must never depend on
one.

**Records are renumbered after either transform.** `index` is the position of a test in the run
that is about to happen — the reporter's `--durations` tie-break reads it, and a serialized report
carries it — so a `--lf` that removed the tests in front of one, or a `--ff` that moved it, has to
renumber rather than leave the collection-time positions behind.
