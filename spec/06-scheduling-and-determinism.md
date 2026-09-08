# 06 — Scheduling, Exclusive Resources, Determinism

*Two premises from the original design list dissolve here rather than being solved: "deterministic
order vs concurrency is a tension" and "exclusive resources risk deadlock". Both dissolve because
explicit DI makes every test's resource footprint statically known (R§6).*

---

## 1. Logical order vs physical order

This split is the whole determinism story.

**Logical order** — the collection order. Sort by `(relative path, definition lineno, param index)`.
Parametrize ids are stable (no `set` iteration, no unpinned `hash()`). Each test gets a dense
integer `index` ([03](03-discovery-and-collection.md)). This is a pure function of the test set.

**Physical order** — dispatch order. Greedy over logical order: repeatedly take the
lowest-index test whose exclusive set is currently free and for which a concurrency slot exists.
A pure function of (test set, seed, and completion timings).

**All output is ordered by logical order** (I2): failure details, short summary, JUnit testcases,
JSON records, `--collect-only`. The result is **byte-identical output run to run**, regardless of
task timing. That is a stronger and more useful guarantee than reproducible *interleaving*, which is
unachievable without a virtual clock — and it is what users actually mean when they say
"deterministic" (R§6).

What is explicitly *not* guaranteed: that two runs interleave identically, or that wall-clock
durations match. `--durations` output is therefore ordered by duration but tie-broken by index.

## 2. Exclusive resources

A fixture may declare `exclusive=True` (token = its own name) or `exclusive="token"`. A test's
**exclusive set** is the union of tokens over the transitive closure of its resolution plan —
computed at collection, stored on the `TestRecord`, never changing.

**Admission rule: all-or-nothing, atomic.** The scheduler admits a test only when its *entire*
exclusive set is free, and acquires the whole set in one synchronous step before the task starts.

This is why deadlock is structurally impossible: there is no hold-and-wait, because no test ever
acquires a token while waiting for another. No lock ordering discipline is needed, no timeout-based
deadlock breaking, no cycle detection at run time. The property is a consequence of static
footprints, which is a consequence of explicit DI — and it is the concrete argument for why
`getfixturevalue`-style dynamic lookup is not in the design ([03](03-discovery-and-collection.md) §5).

This one primitive subsumes xdist's entire scheduling zoo: `loadscope`, `loadfile`, `loadgroup`,
`worksteal` are all blunt proxies for "these tests conflict" (R§6). Here the conflict is declared
where it lives — on the resource — and the scheduler derives the rest.

## 3. The solo tier

Some operations cannot be made task-local at all: `unittest.mock.patch` and friends, `os.environ`
mutation of a variable a library reads at call time, `warnings.filterwarnings` for one test. For
these, a test declares `@voci.solo` — or voci marks it automatically, which is how all stock
`mock.patch` usage is handled in v0.1 ([08](08-patching-and-isolation.md)).

**Model it as a reader-writer lock over the whole suite.** The hazard is racing a *non-patching
reader*, so:

- every normal test implicitly holds a **read lock**;
- a solo test takes the **write lock**: the scheduler drains in-flight tests, runs it alone, then
  resumes.

Cost is honest and visible: the reporter reports the number of solo tests and the wall-clock spent
drained, because a suite that drifts into 200 solo tests has lost the plot and should see that.

`@voci.isolated` is the next tier up (subprocess) and does *not* need the write lock — it is
concurrent with everything, since it shares no process state.

## 4. The scheduler

```
ready      = deque of TestRecords in logical order (index ascending)
in_flight  = {}                        # task -> record
held       = {}                        # token -> record
slots      = Semaphore(concurrency)

loop:
    if solo_pending and in_flight: wait for drain
    scan `ready` from the front, up to LOOKAHEAD (default 256):
        pick the first record whose exclusive set ∩ held == ∅
        and (aging) if a record has been skipped over AGE_LIMIT times, block
        behind it instead of scanning past it
    acquire its tokens atomically, take a slot, dispatch
    on completion: release tokens, release slot, hand result to reporter
```

**Starvation.** A wide-footprint test (many exclusive tokens) can be perpetually passed over by
narrow tests. The aging rule: each time a record is skipped, increment its age; at `AGE_LIMIT`
(default 50) the scheduler stops scanning past it and lets in-flight work drain until its set is
free. Deterministic, because ages are a function of the dispatch sequence, which is itself
deterministic given the same completion order — and where completion order differs, output does not
(I2).

**Seed.** `--seed` only affects tiebreaks in the lookahead scan (and, later, any randomized
ordering mode). Default `0`, so the default schedule is "logical order, greedily packed".

**Bounded lookahead** keeps the scan O(LOOKAHEAD) rather than O(remaining tests) per dispatch — the
scheduler runs thousands of times per suite and must not be O(n²) (I5).

## 5. `--maxfail` and interruption

`-x`/`--maxfail=N` is defined over logical order: **stop dispatching** once N tests have reported a
failing outcome (`failed`, `error`, `timeout`). In-flight tests are cancelled and reported
`interrupted` (R§6). This is documented explicitly, because "the run stopped and 12 tests say
interrupted" is otherwise mysterious — and because some of those interrupted tests might have failed
too (Q13).

Exit code is `2` for an interrupted run even if failures were also recorded, matching pytest.

## 6. Why not work-stealing / bin-packing

The obvious optimization is to schedule long tests first (from the collection cache's duration
history) so the tail doesn't strand. Deliberately deferred:

- it makes physical order depend on *history*, so a fresh checkout schedules differently from a warm
  one — legal under I2 but harder to reason about when debugging a concurrency-sensitive suite;
- the win only matters when a few tests dominate, which `--durations` already surfaces;
- it interacts with aging and exclusivity in ways that need measurement, not intuition.

Roadmap, behind a flag, once the collection cache exists and there is a real suite to measure.

## 7. MVP

Logical-order assignment; greedy bounded-lookahead dispatch with atomic all-or-nothing exclusive
admission; aging; the solo write-lock tier; `--maxfail`/`-x` with cancellation; `--seed`;
`--concurrency=1` as an exact serial mode (same code path, semaphore of 1 — *not* a separate
sequential runner, so the two modes cannot drift).

## 8. Roadmap

- Duration-aware scheduling (longest-first) behind `--schedule=duration`, fed by the collection
  cache.
- Failure-first ordering (`--ff`) as a *logical* order change — note this changes output order too,
  by design, since logical order is the definition.
- Per-token concurrency limits (`exclusive="pool:db", limit=4`) — a counting semaphore instead of a
  mutex, for "at most 4 tests may touch the DB". Straightforward extension of the admission rule and
  a likely early request.
- A `--schedule-trace` dump (which test held which token when) for debugging contention.

## 9. Open questions

- **Q14** — Should `--concurrency=1` also guarantee pytest-like *sequencing* semantics (module scope
  torn down before the next module starts)? It falls out naturally from greedy dispatch in logical
  order, but making it a documented guarantee constrains future scheduler changes. Proposed:
  document it as true-in-practice for `--concurrency=1`, not as a contract.
- **Q15** — Should exclusive tokens be declarable directly on a *test* (`@voci.exclusive("redis")`)
  and not only via fixtures? It is convenient and harmless for scheduling, but it puts resource
  facts somewhere other than the resource. Proposed: yes, as an escape hatch, documented as second
  choice.
