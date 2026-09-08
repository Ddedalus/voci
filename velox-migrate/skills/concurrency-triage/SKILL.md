---
name: concurrency-triage
description: Postfactor triage of a converted velox suite that is green at --serial but not at concurrency. Uses the audit's hazard census (findings.json, `serialized: true` plus VX409-VX413) and its per-finding blast radius to choose, per site, between a dependency seam, @velox.solo, @velox.isolated, and exclusive= on a fixture. Use after `velox-migrate convert --write`, when raising `velox --concurrency` turns tests red or when the audit's percent-of-suite-serialized number needs to come down.
---

# concurrency-triage

Input `findings.json` from the pre-conversion `velox-migrate audit`, plus runs of the converted
suite; output edited **velox** source. Runs after `convert --write`.

Precondition: `velox --serial` is green and matches the pytest baseline test-for-test. If not,
that is a conversion defect, not a concurrency hazard — stop and fix it first.

## Working set

```
jq '[.findings[] | select(.serialized or (.code|startswith("VX41")))]
    | sort_by(-(.tests|length))' .velox-migrate/findings.json
```

`serialized: true` is exactly the set behind `.totals.serialized_percent`, the number this pass
exists to bring down: `VX205`, `VX216`, `VX217`, `VX218`, `VX219`, `VX322`,
`VX401`–`VX408`. `VX409`–`VX413` cost correctness rather than scheduling, so they are in the queue
but not in the percentage.

Fields: `.message` names the exact call (`monkeypatch.chdir`, `os.environ.update`); `.tests` is the
blast radius, computed through the fixture closures, so a call in an autouse fixture already names
every test that inherits it; `.matrix[code].action` is the authoritative remedy prose; `.detail` is
empty for hazards.

`findings.json` describes the **pre-conversion** tree — `.file`/`.line` are pytest paths, and
fixtures have since moved from `conftest.py` to a sibling `fixtures.py`. Match on `.function`, not
on line. There is no re-audit afterwards: `audit` reads a pytest dump and the tree is no longer a
pytest suite.

## Order and the gate rule

Descending `.tests | length`. All the leverage is at the top: one `monkeypatch.setenv` in a root
autouse fixture is a single finding carrying 96% of the suite (`corpus/hazards_showcase`), and
nothing below it moves the percentage until it is dealt with.

> A finding whose site is a fixture and whose `.tests` is large is fixed **at the fixture**. Never
> mark its tests.

Marking `N` tests `@velox.solo` for a hazard in a fixture they share costs `N` serialized tests and
buys nothing — the fixture still writes process-global state. Marks are for a body-level site with
`.tests` of one.

Rule: `len(.tests) > 1`, or `.function` names a fixture factory → seam. Otherwise, the remedy table.

## Remedy

`references/remedies.md` — one row per code: seam, fallback mark, exact spelling.
`references/worked-examples.md` — the shapes that carry most suites.

Cost order, cheapest first; never reach past a row that applies.

1. **Seam** — inject the dependency, or move the value into `[tool.velox] env`. Free at run time.
2. **`exclusive=`** on the fixture owning the contended resource. Only the tests reaching that
   fixture serialize against each other.
3. **`@velox.solo`** — runs with nothing else in flight. Costs the suite's concurrency for that
   test's duration.
4. **`@velox.isolated`** — fresh subprocess, interpreter and loop, on top of solo's scheduling.
   Required, not optional, where the state is interpreter-level and cannot be restored:
   `sys.modules`, `importlib.reload`, `chdir`, locale, decimal context, interpreter limits.

## API

Confirmed against `velox/_marks.py`, `velox/_di/fixtures.py`, `velox/_config.py`:

```python
import velox


@velox.solo  # bare, no parentheses
def test_alone(): ...


@velox.isolated  # bare, no parentheses
def test_in_its_own_subprocess(): ...


@velox.fixture(exclusive=True)  # private token: only this fixture's tests contend
def scratch_dir(): ...


@velox.fixture(exclusive="postgres")  # shared token: every fixture naming it contends
def db(): ...
```

```toml
[tool.velox]
env = { DATABASE_URL = "sqlite://" }   # suite-wide, set once, written by no test
concurrency = 16                       # the default; --concurrency N overrides
```

Decorator order is free — velox reads marks off the function object, and `convert` writes
`@velox.solo` outermost. Neither mark applies to a fixture. `isolated` already implies running
alone, so stacking both is redundant rather than wrong.

## Verify

1. Baseline `velox --serial`; record counts.
2. `velox --concurrency 16`. New failures against the baseline are the queue. A `GlobalPatchError`
   names its own remedy and means a `with mock.patch(...)` in a test `convert` did not mark.
3. Repeat the concurrent run at least three times before believing it — hazards are
   interleaving-dependent and one green run proves nothing.
4. Measure: `rg -c '@velox\.(solo|isolated)'` over the tree against `.totals.serialized_tests`,
   plus `velox` wall clock against `velox --serial`. A pass that traded seams for marks and left
   the wall clock at serial has failed even with a green suite.

## Blind spots

`.blind_spots[]`: `VX414` test-order dependence, `VX415` fixture teardown timing, `VX416` shared
state behind application code (singleton caches, `functools.lru_cache`, ORM identity maps). No
audit can see these, so they appear in no finding and no percentage. A concurrent failure matching
nothing in the working set is almost always one of the three — say so rather than marking the test
`@velox.solo` to make the red go away.
