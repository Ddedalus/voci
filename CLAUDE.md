# velox

Package: `velox/`, nested by subsystem (`_di/`, `_collection/`, `_run/`, `_report/`,
`_builtins/`, `_assertions/`). `__init__.py`, `_version.py`, `cli.py`, `fastapi.py`, `_config.py`
and `_marks.py` stay flat at the package root — `fastapi.py`'s import path (`velox.fastapi`) is
public, and the rest have no roadmap growth pointing at a split. Tests: `tests/`, mirroring the
package (`tests/di/`, `tests/collection/`, ...); a test file that only exercises the public
`velox` surface, not a subpackage's internals, stays flat. Entrypoint: `velox.cli:main`.

Tooling: uv, ruff, pyrefly, pytest. Run via `justfile` — `just list` for recipes
(`sync`, `run`, `test`, `lint`, `fmt`, `typecheck`, `build`, `check`).

## Refactor tools

`justfile` also has AST-based recipes for splitting unrelated objects out of a file into their
own module and repointing their imports:

- `just summarize path` — list a file's top-level objects as `name:start,end` line ranges.
- `just move source destination object...` — cut those objects out of `source`, append them to
  `destination`, copy `source`'s whole import block along for the ride, then `ruff --fix` both
  files so unused imports drop out and needed ones stay.
- `just rewire object old.module new.module` — repoint every top-level `from old.module import
  object` across the repo at `new.module`, preserving aliases, then `ruff --fix` the touched
  files.

Both rewire the mechanical parts of the move: line ranges (decorators included) and imports.
They don't touch a moved object's docstring or `__all__` — add those by hand afterward, same as
any other new module (see the Documentation section below).

**Moving a whole file into a subpackage** (promoting `_x.py` to `_pkg/x.py`, nothing split) is
`git mv`, not `just move` — `move` exists to split objects out of a multi-object file, and running
it on an already-cohesive file re-emits it through ruff for no reason and loses git's rename
tracking. Follow the `git mv` with `just rewire` for every name any other file imports from the
old module. For a consumer that does `from velox import _x` and uses `_x.thing` throughout instead
of `from velox._x import thing`, `rewire` won't touch it (it only follows `from module import
name`) — change just the import line to `from velox._pkg import x as _x`, which rebinds the same
local name so every `_x.thing` call site is untouched.

`rewire` has three sharp edges, found while nesting `velox/`'s own subpackages:
- It only scans a file's *top-level* statements, so an import nested inside `if TYPE_CHECKING:`
  or inside a function body isn't found and needs a manual fix.
- It regenerates the statement fresh rather than patching text in place, so a trailing `# noqa`
  comment on the line gets dropped — check for one before running it on a line that has one.
- Given `from pkg import name`, it can't tell "a submodule literally named `name`, aliased" from
  "an object named `name`" apart — both are the same AST shape. If a subpackage's `__init__.py`
  ever ends up with a real object sharing a name with one of its own submodules, a `rewire` call
  for the object can rewrite the submodule-import line instead. Check the diff.

`rewire` only follows plain `from x import y` clauses (single- or multi-line); dotted `import x`
usage and generated files (like `velox/_assertions/_vendor/_compare_any.py`, whose stand-in
import lives as a string literal in `scripts/vendor_assertion.py`) need a manual fix and a
`just vendor` re-run.

Reference-only, not part of the package: `pytest/`, `fastapi/`, `research/`, `spec/`.
Git submodules for reference: `pytest/`, `fastapi/`.

`velox/_assertions/_vendor/` is **generated** from the `pytest/` submodule — never edit it by
hand. Regenerate with `just vendor` (`scripts/vendor_assertion.py`, which records every edit
in `velox/_assertions/VENDOR.md`); `just vendor-check` verifies the tree is current. It is kept
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

  One exception, and only this one: **public API that a user can apply today and that silently
  does nothing must say so**, in one clause, pointing at `ROADMAP.md` — `"""Mark this test to run
  alone. Not yet enforced; see ROADMAP.md."""`. A docstring that describes the intended behaviour
  of an inert decorator is worse than no docstring, because the user writes the mark and believes
  they are protected. Never let this exception grow into a general status report.
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
- **A docstring states what the thing is. Nothing else.** Not what it *doesn't* do, not what other
  modules do instead, not why it was built this way, not what it replaces, not its implementation
  status, and never how good it is. If a sentence would still be true written on a different file,
  delete it.

### Writing a docstring

Two parts, in order — and most docstrings need only the first:

1. **What it is**, as a noun phrase, in one line. Not "This function returns…", not "Helper
   for…", not "Handles…".
2. **Only what a caller would otherwise get wrong**: an invariant, a lifetime, a live-vs-snapshot
   distinction, an error it raises, an argument that isn't what it looks like. One or two
   sentences. If there is nothing, stop after the first line.

Verify before you write. A docstring is an assertion about behaviour, and a wrong one is worse
than none — read the body, and check that anything it promises is actually enforced somewhere.

The register to match, taken from the current tree:

```python
"""Everything the collector needs to know about a test, read in one attribute lookup."""

"""The marks attached to `fn`, or an empty record. Never raises."""

"""The current test's captured stdout/stderr, live during the test.

Holds a reference to the test's `Sink`, not a snapshot: `.out`/`.err` read straight through
on every access, so text written after this fixture was injected is visible immediately.
"""
```

The failure mode is inflating something unremarkable into a paragraph. This was the worst case,
and every clause after the first line is padding — vague ("what `assert` alone cannot express"),
a comparison to other modules, a status boast, and a justification:

```python
"""Assertion helpers.

The primary assertion mechanism is plain `assert`, rewritten for introspection. These two
cover what `assert` alone cannot express.

Unlike the rest of the package, these are implemented rather than stubbed: they are pure,
they depend on nothing in the runtime, and having them work makes the examples readable.
"""
```

It became:

```python
"""`raises` and `approx`: the two assertion helpers velox provides.

`raises` is a context manager that catches an expected exception and exposes it as
`ExceptionInfo`, optionally matching the exception type and a regex against its message.
`approx` wraps a number, or a collection of numbers, for tolerant `==` comparison.
"""
```

`docs/M1-PLAN.md` is the internal build log — the one place where milestone vocabulary, commit
pointers, and per-slice history belong. Nothing else links to it.
