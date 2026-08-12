---
name: refactor-tools
description: AST-based justfile recipes for splitting objects out of a velox source file into their own module and repointing imports across the repo. Use when moving, splitting, or renaming top-level functions/classes/objects within velox/, or when asked to "move X out of Y" or "split this file".
---

# velox refactor tools

`justfile` has AST-based recipes for splitting unrelated objects out of a file into their own
module and repointing their imports. Run `just list` for the full recipe list.

## `just summarize path`

Lists a file's top-level objects as `name:start,end` line ranges. Run this first to get exact
object names before a `move`.

## `just move source destination object...`

Cuts the named objects out of `source`, appends them to `destination`, copies `source`'s whole
import block along for the ride, then `ruff --fix` both files so unused imports drop out and
needed ones stay.

## `just rewire object old.module new.module`

Repoints every `from old.module import object` clause across the repo (top-level, or nested
inside `if TYPE_CHECKING:` or a function) at `new.module`, preserving aliases and a trailing
`# noqa` comment, then `ruff --fix`s the touched files. Prints a `- old line` / `+ new line` diff
for every rewrite — read it, don't trust it blindly (see sharp edge below).

## `just rewire-module parent name new_parent new_name`

For a consumer that does `from parent import name` (a whole submodule, e.g. `from velox import
_capture`) and uses `name.thing` throughout rather than importing specific names, repoints just
that import at `from new_parent import new_name as name`, so every existing `name.thing` call
site stays untouched. Same diff output as `rewire`.

## What none of these do

None of the three touch a moved object's docstring or `__all__` — add those by hand afterward,
same as any other new module. `rewire`/`rewire-module` only follow `from x import y` clauses
(single- or multi-line); dotted `import x` usage and generated files (like
`velox/_assertions/_vendor/_compare_any.py`, whose stand-in import lives as a string literal in
`scripts/vendor_assertion.py`) need a manual fix and a `just vendor` re-run.

## Moving a whole file into a subpackage

Promoting `_x.py` to `_pkg/x.py` with nothing split is `git mv`, not `just move` — `move` exists
to split objects out of a multi-object file, and running it on an already-cohesive file re-emits
it through ruff for no reason and loses git's rename tracking. Follow the `git mv` with
`rewire`/`rewire-module` for every name or whole-module import any other file uses.

## `rewire`'s sharp edge

Given `from pkg import name`, `rewire` can't tell "a submodule literally named `name`, aliased"
(use `rewire-module` for this) from "an object named `name`" (use `rewire`) apart — both are the
same AST shape. Pointing `rewire` at a name that's actually a submodule import rewrites that
import's *source*, silently producing a self-referential or wrong import. This is exactly what
the printed diff is for: read it before moving on, especially when a name could plausibly be
either.
