# `_report/terminal.py` — reporting

See [rationale.md](../rationale.md) for the index.

**The reporter counts records, it does not build a dict.** A factory-generated test can repeat its
id, so two records may legitimately share one id within a file. Deriving per-file counts from a
dict keyed by id collapses those, undercounts the file, and flushes its scrollback block early —
before every test has reported in.

**`stream` is bound once, at construction.** By the time results arrive, `sys.stdout` has been
replaced by the capture `Router` and the current test's context has been reset. A `print` that
resolved `sys.stdout` lazily would land in the session sink, and every per-file block would
disappear from live output and resurface at the end under unattributed output — with nothing about
the symptom pointing back here.

**A default run's length is bounded by how many files it collected, not how many tests it
skipped.** A skip contributes a count to its file's block; the reasons are a `-v` section. Those
reasons are worth reading once, when a skip is written or when someone goes looking for it, and
never on the hundred runs in between — a suite with fifty long-standing skips otherwise buries its
file blocks under fifty lines that say nothing new about this run.

**The counts land in one place, once.** Only the reporter prints them, and only at the end: the
per-outcome breakdown, the totals, and the wall-vs-concurrency ratio are all one derivation of one
result list. Two summaries computed from the same run in two modules will eventually disagree about
what "failed" means, and a reader with two lines of counts in front of them has to work out which
one is the answer.

**Zero counts are left out.** A category prints only when it has something to report, so a clean
run's totals carry the four or five numbers that describe it rather than every bucket velox knows
about. What survives is the line's real job: the totals a reader checks at a glance, and — on a run
with failures — a line above it holding nothing but what went wrong.

**The path column is measured once, from every path the run collected.** Every file is known before
the first block prints, so the column can be sized to the deepest path in the suite instead of to a
constant that a real tree outgrows on its first `tests/api/v2/` directory. It is measured up front
rather than per block because widening it mid-run would leave every block already on screen ragged
against the ones below it, and clamped at both ends so a shallow suite still gets a column and a
pathological path is elided rather than pushing the durations off the terminal.
