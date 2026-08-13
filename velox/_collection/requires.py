"""`velox.use(...)`: fixtures a container declares on behalf of the tests inside it.

A test module that calls `velox.use(db_reset)` gives every test it defines an extra dependency on
`db_reset`, without any of those tests naming it. The fixture is an imported object, exactly as at
a `Depends(...)` site — the declaration just lives on the container rather than on each signature,
and the value is discarded.

The call writes a tuple onto the enclosing module, which `collect` reads back with `requires_of`
and hands to `_fixtures.plan_for` as its `implicit` argument. Declarations accumulate: several
calls in one module apply in source order.
"""

from __future__ import annotations

import sys
from typing import Any

from velox._di.fixtures import Fixture

__all__ = ["REQUIRES_ATTR", "requires_of", "use"]

#: Where `use` stores a container's declared fixtures. Part of the tested surface, the same way
#: `_marks.MARKS_ATTR` is.
REQUIRES_ATTR = "__velox_requires__"


def use(*fixtures: Fixture[Any]) -> None:
    """Declare fixtures every test in the enclosing module depends on.

    Called as a statement in a module body, alongside the tests it applies to. Each fixture is
    constructed before the test's own dependencies and torn down after them, and its value is
    never passed to the test. Raises `TypeError` for an argument that isn't a `Fixture`, or for a
    call anywhere but a module body.
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
            "velox.use() applies to every test in a module, so it belongs in a module body, "
            f"not inside {frame.f_code.co_qualname!r}."
        )
    return frame.f_globals
