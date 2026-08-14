"""`unittest.mock` patching: finding it on a test, and catching it as it installs.

`mock.patch` installs by writing to a module or class attribute, which every concurrently
running test sees, so a test that patches runs alone. The decorator form is visible on the
function object -- `mock.patch` records a `patchings` list on the wrapper it returns, and
`mock.patch.dict` closes over its patcher -- and `patching_of` reads both off the
`__wrapped__` chain, together with how many leading positional parameters those patchers
fill in at call time. `with mock.patch(...)` inside a body leaves nothing to find, so it is
caught where it installs instead: `install()` guards `unittest.mock`'s own `__enter__` for
the duration of a run and raises `GlobalPatchError` for a patch entered from a test that
isn't running alone.
"""

from __future__ import annotations

import functools
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from velox._builtins.capture import current_test_context

__all__ = [
    "GlobalPatchError",
    "Patching",
    "install",
    "installed",
    "patching_of",
    "real_function",
    "uninstall",
]


@dataclass(frozen=True, slots=True)
class Patching:
    """The `unittest.mock` patching a test function carries, read off the function object."""

    targets: tuple[str, ...]
    """Display name per patcher, in the order `unittest.mock` applies them. Empty for a test
    that patches nothing, which is what `_run.run` reads to decide whether to schedule the
    test alone."""
    positional_args: int
    """How many leading positional parameters the patchers pass their mock objects to --
    what `_di.fixtures.plan_for` needs so those parameters don't read as missing injections."""
    keyword_args: frozenset[str] = frozenset()
    """Parameters the patchers pass their mock objects to by name instead, as
    `mock.patch.multiple` does. Supplied at call time exactly the way `@velox.parametrize`'s
    own arguments are, and handed to collection the same way."""


NO_PATCHING = Patching(targets=(), positional_args=0)


def patching_of(func: Callable[..., Any]) -> Patching:
    """The `unittest.mock` patching decorating `func`, or `NO_PATCHING`. Never raises.

    Nothing can be patching when `unittest.mock` was never imported, which is the whole cost
    of this call for a suite that doesn't mock.
    """
    mock = sys.modules.get("unittest.mock")
    if mock is None:
        return NO_PATCHING
    patch_type = getattr(mock, "_patch", None)
    dict_patch_type = getattr(mock, "_patch_dict", None)
    default = getattr(mock, "DEFAULT", None)
    if patch_type is None or dict_patch_type is None:
        return NO_PATCHING

    targets: list[str] = []
    keyword_args: set[str] = set()
    positional_args = 0
    for level in _wrapper_chain(func):
        for group in getattr(level, "patchings", None) or ():
            if not isinstance(group, patch_type):
                continue
            for patcher in _group_members(group):
                targets.append(_target_name(patcher))
                if patcher.new is not default:
                    continue  # `new=` named the replacement; the test is passed nothing
                # `mock.patch.multiple` names the parameter it fills, every other form
                # appends its mock to the positional arguments the test is called with.
                if patcher.attribute_name is None:
                    positional_args += 1
                else:
                    keyword_args.add(patcher.attribute_name)
        # `mock.patch.dict` records no `patchings`: its decorator is a closure over the
        # patcher, so the closure is where it can be found. It passes no mock object in.
        for value in _closure_values(level):
            if isinstance(value, dict_patch_type):
                targets.append(_target_name(value))
    return Patching(
        targets=tuple(targets),
        positional_args=positional_args,
        keyword_args=frozenset(keyword_args),
    )


def _group_members(patcher: Any) -> Iterator[Any]:
    """`patcher`, then the patchers it leads. `mock.patch.multiple` records one patcher per
    call, holding the rest of its group; every other form is a group of one."""
    yield patcher
    yield from getattr(patcher, "additional_patchers", None) or ()


def real_function(func: Callable[..., Any]) -> Callable[..., Any]:
    """The function underneath `func`'s `functools.wraps`-style decorators, or `func` itself.

    A decorator's wrapper takes `(*args, **kwargs)`, so this is the object to read a test's
    injection plan and its definition line off; the wrapper is still what velox calls. Only a
    real function is ever returned -- a `__wrapped__` pointing at something else (a mock, an
    instance) leaves the innermost function above it as the answer.
    """
    real = func
    for level in _wrapper_chain(func):
        if hasattr(level, "__code__"):
            real = level
    return real


def _wrapper_chain(func: Callable[..., Any]) -> Iterator[Callable[..., Any]]:
    """`func`, then each `__wrapped__` under it, down to the real function."""
    seen: set[int] = set()
    current: Any = func
    while callable(current) and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = getattr(current, "__wrapped__", None)


def _closure_values(func: Callable[..., Any]) -> Iterator[Any]:
    """Every bound cell of `func`'s closure, skipping cells not yet filled."""
    for cell in getattr(func, "__closure__", None) or ():
        try:
            yield cell.cell_contents
        except ValueError:  # an empty cell, e.g. a recursive closure mid-definition
            continue


def _target_name(patcher: Any) -> str:
    """What one patcher patches, for the report: the attribute name, or the dictionary's
    type for `mock.patch.dict`. Best effort -- `mock.patch` resolves the target module only
    once the patch is entered."""
    attribute = getattr(patcher, "attribute", None)
    if isinstance(attribute, str):
        return attribute
    in_dict = getattr(patcher, "in_dict", None)
    if isinstance(in_dict, str):
        return in_dict
    if in_dict is not None:
        return f"{type(in_dict).__module__}.{type(in_dict).__qualname__}"
    return "unittest.mock patch"


class GlobalPatchError(RuntimeError):
    """A `unittest.mock` patch entered from a test velox is running alongside others.

    Raised by the guard `install()` puts on `unittest.mock`, at the `with mock.patch(...)`
    line and before anything is written, so the patch the message names never reaches the
    module or class it targets.
    """


#: What `uninstall` puts back: `(class, original __enter__)` per guarded class, empty when no
#: guard is installed. Module-global because the guard it tracks is process-global too.
_restore: list[tuple[type, Any]] = []
_active = False


def install() -> bool:
    """Guard `unittest.mock`'s patch installation for the rest of the run, and report whether
    this call is the one that installed it -- only that caller may `uninstall`, so a nested run
    can't disarm the guard the run around it is relying on.

    A no-op unless the suite has imported `unittest.mock`, and a no-op for a stdlib whose
    internals have moved -- both leave patching undetected rather than failing a run over it.
    """
    global _active
    if _active:
        return False
    mock = sys.modules.get("unittest.mock")
    if mock is None:
        return False
    _active = True
    for name in ("_patch", "_patch_dict"):
        patch_type = getattr(mock, name, None)
        original = getattr(patch_type, "__enter__", None)
        if patch_type is None or original is None:
            continue
        patch_type.__enter__ = _guarded(original)
        _restore.append((patch_type, original))
    return True


def uninstall() -> None:
    """Restore whatever `install` guarded. Idempotent."""
    global _active
    for patch_type, original in _restore:
        patch_type.__enter__ = original
    _restore.clear()
    _active = False


def installed() -> bool:
    """Whether the guard is currently installed."""
    return _active


def _guarded(original: Any) -> Any:
    @functools.wraps(original)
    def __enter__(patcher: Any) -> Any:
        _check(patcher)
        return original(patcher)

    return __enter__


def _check(patcher: Any) -> None:
    """Raise unless the test entering `patcher` -- if a test is entering it at all -- is one
    velox scheduled to run by itself."""
    context = current_test_context.get()
    if context is None or context.patching_allowed:
        return
    target = _target_name(patcher)
    # First line stands alone: it is what the short test summary shows
    # (`_run.run._summarize_exception` keeps only that much).
    raise GlobalPatchError(
        f"{target} was patched by a test that isn't running alone.\n\n"
        f"unittest.mock installs a patch by writing to the module or class itself, so every "
        f"test running at the same time sees it. Mark this test @velox.solo to run it alone, "
        f"or @velox.isolated to run it in its own subprocess. A patch applied as a decorator "
        f"is found at collection and scheduled alone for you."
    )
