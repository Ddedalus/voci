# velox

Package: `velox/` (flat layout). Tests: `tests/`. Entrypoint: `velox.cli:main`.

Tooling: uv, ruff, pyrefly, pytest. Run via `justfile` — `just list` for recipes
(`sync`, `run`, `test`, `lint`, `fmt`, `typecheck`, `build`, `check`).

Reference-only, not part of the package: `pytest/`, `fastapi/`, `research/`, `spec/`.
Git submodules for reference: `pytest/`, `fastapi/`.

`velox/_vendor/assertion/` is **generated** from the `pytest/` submodule — never edit it by
hand. Regenerate with `just vendor` (`scripts/vendor_assertion.py`, which records every edit
in `velox/_vendor/VENDOR.md`); `just vendor-check` verifies the tree is current. It is kept
byte-identical to upstream and excluded from ruff and pyrefly. The one hand-written file there
is `_shim.py`.

## Documentation

Docs are a product surface, not a build log. A reader arrives knowing nothing about how velox was
built and should never have to learn.

### Where each kind of information lives

| Layer | Holds | Budget |
|---|---|---|
| `README.md` | What velox is, who it's for, install, first test, links out | 2 paragraphs + 2 snippets before anyone scrolls |
| `ROADMAP.md` | What isn't built yet, grouped and honest. **The only place unimplemented behaviour is described.** | short bullets |
| `docs/rationale.md` | The WHY — decisions a maintainer would otherwise reverse by accident. Sections keyed to code files. | a few paragraphs per decision, top decisions only |
| `docs/*.md` | Feature guides and usage patterns, as features mature | — |
| `examples/*/README.md` | What this example demonstrates, how to run it, what to look at | ~40 lines |
| Module docstring | WHAT this module is and how it fits the pipeline | ≤ 10 lines |
| Class docstring | What the type represents and its invariants | ≤ 5 lines |
| Function docstring | Only when the signature doesn't already say it | ≤ 3 lines |
| Inline comment | Why this code is weird, non-obvious, or load-bearing in a surprising way | 1–3 lines |

Between file and class docstrings a reader should get a good picture of WHAT is going on, without
reading the bodies.

### Rules

- **Never cite `spec/` outside `spec/`.** It is a scratch design artifact used to build the code,
  full of options and trade-offs nobody downstream cares about. It is not published and will be
  deleted. Same for `docs/M1-PLAN.md`, milestone names (M0/M1/M2/M3), "slice", "session", and
  "this milestone". If a spec passage is genuinely load-bearing, move the *conclusion* into
  `docs/rationale.md` in your own words and cite nothing.
- **No counterfactuals.** Don't document what the code doesn't do, what a future version might do,
  what was considered and rejected, or what another component defers. Describe what is there.
  Everything unbuilt goes in `ROADMAP.md`, once.
- **Write as if the code was always this way.** Ban "now", "still", "no longer", "used to",
  "instead of before", "deliberately not", "this replaces".
- **Don't argue with pytest.** One neutral comparison in `README.md` sets expectations; that's the
  budget. Module docstrings describe velox, not the alternative. Naming a concrete upstream
  behaviour is fine when it explains a real constraint on *this* code — scoring points is not.
- **Comments explain why, not what.** If a comment restates the line below it, delete it. If the
  explanation runs past ~3 lines, it's rationale — move it to `docs/rationale.md` and leave a
  one-line pointer.
- **Docstrings are prose, not slide decks.** No bold-heading walls, no ASCII diagrams of code that
  is right there, no bulleted inventories of what a module contains.

`docs/M1-PLAN.md` is the internal build log — the one place where milestone vocabulary, commit
pointers, and per-slice history belong. Nothing else links to it.
