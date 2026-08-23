---
name: unwind-override
description: Prefactor a pytest conftest fixture override that velox-migrate refuses (VX006 over-budget chain, VX027 autouse in chain, VX028 ambiguous getfixturevalue) or specializes more widely than wanted (VX005). Rewrites the override in pytest terms, with pytest as the oracle, so the next `velox-migrate convert` run converts it. Use when `findings.json` carries one of those codes, while the suite is still green under pytest and before anything is converted.
---

# unwind-override

Input `findings.json` from `velox-migrate audit`; output edited **pytest** source; oracle `pytest`
staying green. Runs before `velox-migrate convert`, never after.

## Trigger

| Code | Why it needs you |
|---|---|
| `VX006` | Chain over the fan-out budget. `convert` refuses the override, every fixture downstream of it, and every test reaching those. |
| `VX027` | An autouse fixture sits in the chain. Two definitions of one autouse name cannot both be `velox.use(...)`d over the same tests, so this is refused at any budget. |
| `VX028` | `request.getfixturevalue("x")` where the override gives `x` two meanings, so no parameter can carry it. |
| `VX005` | Converts mechanically, but plants `fan_out` copied fixtures in the diff. Unwind only if a smaller diff was asked for. |

## Input

```
jq '.findings[] | select(.code=="VX006" or .code=="VX027")' .velox-migrate/findings.json
```

| Path | Meaning |
|---|---|
| `.file`, `.line`, `.function` | The overriding `def`. `.file` is rootdir-relative, `.line` 1-based. |
| `.detail.fixture` | The overridden argname. |
| `.detail.scope` | The visibility node, **rendered for a reader** — a directory path, or the literal `the suite root` for `.`/`""`. Not a raw path; get that from the dump. |
| `.detail.fan_out` | One plus the size of `D` (below). What the budget is spent against. |
| `.detail.budget` | The budget this audit used. Re-audit with the same one. |
| `.detail.duplicated` | Comma-joined argnames of `D`; `""` at fan-out 1. |
| `.tests` | Node ids resolving through this override. |
| `.message` | Names the node the base definition came from. |

Chains, definition sites and reach come from `.velox-migrate/ground-truth.json` — paths and a
runnable query in `references/chain-analysis.md`.

## The number to move

`D` = fixtures that (a) some test in `.tests` reaches, (b) transitively depend on the overridden
name, and (c) are defined **outside** the override's visibility node. A fixture at or below the
node is wired to the override, never copied for it, so it is not in `D` at any size.

That leaves exactly three levers, and the strategies are those three: stop overriding the name;
move a member of `D` to at-or-below the node; shorten the dependency chain above the node.

## Choose

First row that holds. Before/after for each in `references/strategies.md`.

| # | Strategy | Holds when | fan_out |
|---|---|---|---|
| 0 | **De-autouse** | `VX027`, always, first. Drop `autouse=True` across the chain, name the fixture in module-level `pytestmark = pytest.mark.usefixtures(...)`. Re-audits as `VX005`/`VX006`; continue below. | unchanged |
| 1 | **Delete the override** | The base can produce a value every test accepts, in and out of the subtree. | → 0 |
| 2 | **Move `D` down** | Per member: the query in `references/chain-analysis.md` reports it `MOVABLE`. | −1 each |
| 3 | **Rename and request** | The subtree wants a genuinely different object, and `D` has at most two members. | → 0 |
| 4 | **Factory seam** | `D` is large and its members hold real bodies. Lift bodies to a plain helper module first, then apply 3. | → 0, via 3 |
| 5 | **Narrow the node** | Only some tests under the node need the override. Shrinks `.tests`, and re-runs 2's `MOVABLE` check against a smaller set. | −1 per member |

None applies → stop and report. `VX006` is a legitimate refusal; a chain that will not unwind is
one to convert by hand.

## Procedure

1. `pytest` green first. Record the pass/fail/skip counts — they are the assertion.
2. One override per iteration. Unwinding an outer override changes an inner one's chain.
3. Edit. Run `pytest <ids from .tests>`, then the whole suite. Counts must match step 1 exactly;
   an outcome that moved, skip→pass included, is a semantic change and gets reverted.
4. Re-extract *then* re-audit, at the same budget. `audit` reads the dump, not the tree, so the
   dump is stale the instant a conftest changes:
   `velox-migrate extract && velox-migrate audit --budget <detail.budget>`
5. Confirm the code is gone from `.findings[].code` and `.totals.blocked_tests` did not grow. A
   new `VX028`, `VX012` or `VX003` is a regression from the edit.

## Constraints

- Emit pytest only. No `import velox`, no `@velox.*`, no `Depends()`. The suite is still a pytest
  suite and the entire verification story rests on that.
- Never change what a fixture produces. Green pytest is the only oracle, and a value change the
  suite happens to tolerate is the silent-meaning-change failure this pass exists to prevent.
- A fixture reached by tests outside `.tests` is not yours to move or rename; check reach first.
- `def settings(settings)` (the `[-2]` super pattern) stays legal after strategies 1, 2 and 5.
  Strategies 3 and 4 must resolve it by hand — a renamed fixture cannot request its own old name.
