"""`velox.use(...)`: fixtures a container declares on behalf of the tests inside it.

A test module that calls `velox.use(db_reset)` gives every test it defines an extra dependency on
`db_reset`, without any of those tests naming it. A package `__init__.py` that calls it does the
same for every test in that directory and below. The fixture is an imported object, exactly as at
a `Depends(...)` site — the declaration just lives on the container rather than on each signature,
and the value is discarded.

The call writes a tuple onto the enclosing module, which `collect` reads back with `requires_of`
and hands to `_fixtures.plan_for` as its `implicit` argument. `package_inits` gives `collect` the
enclosing packages to read it back from, outermost first. Declarations accumulate: several calls
in one module apply in source order, and a package's apply ahead of those of the modules under it.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from velox._di.fixtures import Fixture

__all__ = ["REQUIRES_ATTR", "combined", "package_inits", "requires_of", "use"]

#: Where `use` stores a container's declared fixtures. Part of the tested surface, the same way
#: `_marks.MARKS_ATTR` is.
REQUIRES_ATTR = "__velox_requires__"


def use(*fixtures: Fixture[Any]) -> None:
    """Declare fixtures every test in the enclosing container depends on.

    Called as a statement in a module body — a test module, or a package `__init__.py`, which
    reaches every test in that directory and below. Each fixture is constructed before the test's
    own dependencies and torn down after them, and its value is never passed to the test. Raises
    `TypeError` for an argument that isn't a `Fixture`, or for a call anywhere but a module body.
    """
    for fixture in fixtures:
        if not isinstance(fixture, Fixture):
            raise TypeError(
                f"velox.use() takes fixtures, got {type(fixture).__name__} -- pass the function "
                f"decorated with @velox.fixture(), not a call to it."
            )
    namespace = _declaring_namespace()
    namespace[REQUIRES_ATTR] = (*namespace.get(REQUIRES_ATTR, ()), *fixtures)


def requires_of(module: object) -> tuple[Fixture[Any], ...]:
    """The fixtures `module` declared with `velox.use(...)`, in declaration order. Never raises."""
    namespace = getattr(module, "__dict__", None)
    if namespace is None:
        return ()
    return namespace.get(REQUIRES_ATTR) or ()


def package_inits(path: Path, rootdir: Path) -> tuple[Path, ...]:
    """The `__init__.py` files of the packages containing `path`, outermost first.

    Walks up from `path`'s own directory and stops at the first directory without an
    `__init__.py`, so the result is the unbroken package chain a Python import would traverse.
    `rootdir` is the far end of the walk, and nothing outside it is ever read: a `path` that isn't
    under `rootdir` at all contributes at most the package it sits in directly. `path` itself is
    never in the result, so naming an `__init__.py` as a test file reads its declarations once,
    off the module, rather than a second time as its own container. Returns resolved, absolute
    paths that exist.
    """
    path = Path(path).resolve()
    directory = path.parent
    rootdir = Path(rootdir).resolve()
    inits: list[Path] = []
    while (init := directory / "__init__.py").is_file():
        if init != path:
            inits.append(init)
        if directory == rootdir or rootdir not in directory.parents:
            break
        directory = directory.parent
    inits.reverse()
    return tuple(inits)


def combined(*declarations: Sequence[Fixture[Any]]) -> tuple[Fixture[Any], ...]:
    """`declarations` concatenated, keeping the earliest occurrence of a fixture object declared
    more than once. A `scope="call"` fixture is otherwise built once per declaration naming it,
    since it is the one scope with no single-flight cache to fold the repeats back together."""
    seen: dict[int, Fixture[Any]] = {}
    for group in declarations:
        for fixture in group:
            seen.setdefault(id(fixture), fixture)
    return tuple(seen.values())


def _declaring_namespace() -> dict[str, Any]:
    """The globals of the module body calling `use`.

    Reads the caller's frame rather than taking the module as an argument, so the declaration
    reads as one statement with nothing to repeat. `TypeError` for any other kind of frame: a
    `use()` inside a function would run too late to reach collection, and silently doing nothing
    is the one outcome worse than refusing.
    """
    frame = sys._getframe(2)
    if frame.f_code.co_name != "<module>":
        raise TypeError(
            "velox.use() applies to every test in the container declaring it, so it belongs in a "
            f"module body, not inside {frame.f_code.co_qualname!r}."
        )
    return frame.f_globals
