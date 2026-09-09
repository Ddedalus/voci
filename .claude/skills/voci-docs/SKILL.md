---
name: voci-docs
description: Documentation style rules - where each kind of information belongs and how to write - from inline comments to user-facing docs.
---

# voci documentation

Docs are a product surface, not a build log. Readers neither know nor care about how voci was built.

All new docs prose needs to go through review against [./ClaudishToEnglish.md] to kills any slop Claude idioms.

## Where information lives

| Layer | Holds | Budget |
|---|---|---|
| `README.md` | What voci is, install, first test, links out | 2 paragraphs + 2 snippets |
| `ROADMAP.md` | Unbuilt behaviour. **Only place it's described.** | short bullets |
| `plans/rationale/` | WHY, keyed to files — decisions a maintainer would else reverse | top decisions only, one file per module |
| `docs/*.md` | Feature guides, usage patterns | — |
| `examples/*/README.md` | What it shows, how to run it | ~40 lines |
| Module docstring | WHAT it is, its place in the pipeline | ≤10 lines |
| Class docstring | Type + invariants | ≤5 lines |
| Function docstring | Only what the signature doesn't say | ≤3 lines |
| Inline comment | Why code is non-obvious/load-bearing | 1–3 lines |

## Rules

- **Never cite `spec/`, `docs/M1-PLAN.md`, or milestone names** (M0/M1/M2, "slice", "session")
  outside those files. Load-bearing spec content → restate the *conclusion* in `plans/rationale/`
  (the file for the module it concerns, or `global.md`).
- **No counterfactuals**: describe what's there, not what it doesn't do, might do later, or was
  rejected. Unbuilt → `ROADMAP.md`, once. Exception: a public API that silently no-ops today must
  say so in one clause pointing at `ROADMAP.md` — don't expand this into a status report.
- **Write as if the code was always this way** — no "now"/"still"/"no longer"/"used to"/"this
  replaces".
- **Don't argue with pytest** beyond one neutral README comparison; name upstream behaviour only
  to explain a real constraint on *this* code.
- **Comments explain why, not what.** Delete ones restating the line below; move >3-line reasoning
  to the module's file in `plans/rationale/` with a pointer.
- **Prose, not slide decks** — no bold-heading walls, ASCII diagrams, bulleted inventories.
- **A docstring states what the thing is, nothing else** — not what it doesn't do, what other code
  does, why it was built that way, or its status. If true of any file, delete it.

## Writing a docstring

1. **What it is**, one line, noun phrase — not "This function returns…"/"Helper for…".
2. **Only what a caller would get wrong**: invariant, lifetime, live-vs-snapshot, a raised error,
   a deceptive argument. One or two sentences; skip if there's nothing.

Verify before writing — a wrong docstring is worse than none; confirm anything promised is
actually enforced. Failure mode: inflating something unremarkable with vague filler, comparisons,
or justifications — keep only the noun phrase and the caller-relevant sentence.
