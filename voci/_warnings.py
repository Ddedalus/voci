"""Warning filters, and the record of what a run's tests warned about.

Every warning raised during a run goes through one `warnings.showwarning` shim installed here,
which decides what to do with it against a filter stack rather than against `warnings.filters`.
That list is process-global, so a per-test filter written into it would apply to every other
test dispatched alongside; the shim reads the filters of whichever test raised the warning off
a `ContextVar` instead. `install` owns the process-global warning state for the run and holds
the session's own filters -- `[tool.voci] filterwarnings` and `-W` -- and `collecting` layers
one test's filters over them and gathers what that test raised. `_run.run` wires both up;
`_run.safety` registers the hook that claims un-awaited-coroutine warnings before any of this
sees them.
"""

from __future__ import annotations

import builtins
import contextlib
import importlib
import re
import sys
import threading
import warnings
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, final

__all__ = [
    "DEFAULT_ACTION",
    "MAX_DISTINCT_WARNINGS",
    "Action",
    "FilterError",
    "RecordedWarning",
    "SwallowHook",
    "WarningFilter",
    "collecting",
    "install",
    "parse_filter",
    "parse_filters",
    "register_swallow",
    "session_warnings",
    "uninstall",
    "unregister_swallow",
]

type Action = Literal["default", "always", "ignore", "module", "once", "error"]

#: In the order `-W`'s own abbreviations resolve against: `-W e` is `error`, `-W m` is `module`.
_ACTIONS: tuple[Action, ...] = ("default", "always", "ignore", "module", "once", "error")

#: What a warning no filter matches gets. Every warning is reported rather than dropped: a run
#: that silences a deprecation by default is a run that never tells anyone about it.
DEFAULT_ACTION: Action = "always"

#: How many *distinct* warnings one test (or the session) records before it stops taking new
#: ones. A repeat of one already recorded is free -- it increments a count -- so this bounds
#: only a test that manages to warn about a genuinely new thing thousands of times.
MAX_DISTINCT_WARNINGS = 1000


class FilterError(ValueError):
    """A filter spec that can't be parsed: an unknown action or category, a bad regex, or more
    than the five `action:message:category:module:lineno` fields.

    `spec` is the text that couldn't be parsed, so a caller holding several tiers of specs can
    say which of them wrote it.
    """

    def __init__(self, spec: str, problem: str) -> None:
        super().__init__(f"{spec!r}: {problem}")
        self.spec = spec


@final
@dataclass(frozen=True, slots=True)
class WarningFilter:
    """One parsed filter spec. `message` and `module` are regexes matched against the start of
    the warning's text and of the raising module's dotted name; `lineno` of 0 matches any line.
    """

    action: Action
    message: re.Pattern[str] | None
    category: type[Warning]
    module: re.Pattern[str] | None
    lineno: int
    #: The spec this was parsed from, kept so an error about it can quote what the user wrote.
    spec: str

    def matches(self, text: str, category: type[Warning], module: str, lineno: int) -> bool:
        """Whether this filter governs a warning, tested the way CPython tests its own."""
        return (
            (self.message is None or self.message.match(text) is not None)
            and issubclass(category, self.category)
            and (self.module is None or self.module.match(module) is not None)
            and (self.lineno == 0 or self.lineno == lineno)
        )


@final
@dataclass(frozen=True, slots=True)
class RecordedWarning:
    """One warning a run let through, with how many times it was raised.

    `count` climbs only under the `always` action; every other action records a warning once
    and stops counting it.
    """

    category: str
    message: str
    filename: str
    lineno: int
    count: int = 1

    @property
    def location(self) -> str:
        return f"{self.filename}:{self.lineno}"


type SwallowHook = Callable[[Warning | str, type[Warning], str, int], bool]
"""What `register_swallow` takes: a hook that claims a warning outright, returning whether it
did. A claimed warning is neither filtered nor recorded."""


def parse_filter(spec: str) -> WarningFilter:
    """`action:message:category:module:lineno`, with every field but the first optional.

    `message` and `module` are regexes (not escaped literals) matched against the start of the
    warning's text and of the raising module's dotted name. Raises `FilterError`.
    """
    parts = spec.split(":")
    if len(parts) > 5:
        raise FilterError(
            spec, "too many fields -- expected at most action:message:category:module:lineno"
        )
    parts += [""] * (5 - len(parts))
    action_text, message, category_text, module, lineno_text = (part.strip() for part in parts)
    return WarningFilter(
        action=_action(action_text, spec=spec),
        message=_pattern(message, field="message", spec=spec, flags=re.IGNORECASE),
        category=_category(category_text, spec=spec),
        module=_pattern(module, field="module", spec=spec, flags=0),
        lineno=_lineno(lineno_text, spec=spec),
        spec=spec,
    )


def parse_filters(specs: Sequence[str]) -> tuple[WarningFilter, ...]:
    """`parse_filter` over a sequence, keeping the order it was written in: later specs win."""
    return tuple(parse_filter(spec) for spec in specs)


def _action(text: str, *, spec: str) -> Action:
    """An action name or any unambiguous prefix of one; empty means `default`."""
    if not text:
        return "default"
    for action in _ACTIONS:
        if action.startswith(text):
            return action
    raise FilterError(spec, f"unknown action {text!r} (one of {', '.join(_ACTIONS)})")


def _pattern(text: str, *, field: str, spec: str, flags: int) -> re.Pattern[str] | None:
    if not text:
        return None
    try:
        return re.compile(text, flags)
    except re.error as exc:
        raise FilterError(spec, f"{field} {text!r} is not a valid regex: {exc}") from exc


def _category(text: str, *, spec: str) -> type[Warning]:
    """A warning class by name: a builtin one bare (`DeprecationWarning`), anything else by its
    dotted import path. Importing it here rather than on first match is what makes a typo an
    error where the filter was written."""
    if not text:
        return Warning
    module_name, _, class_name = text.rpartition(".")
    if module_name:
        try:
            found: object = getattr(importlib.import_module(module_name), class_name)
        except (ImportError, AttributeError) as exc:
            raise FilterError(spec, f"can't resolve warning category {text!r}: {exc}") from exc
    else:
        found = getattr(builtins, class_name, None)
        if found is None:
            raise FilterError(spec, f"unknown warning category {text!r}")
    if not (isinstance(found, type) and issubclass(found, Warning)):
        raise FilterError(spec, f"{text!r} is not a Warning subclass")
    return found


def _lineno(text: str, *, spec: str) -> int:
    if not text:
        return 0
    try:
        lineno = int(text)
    except ValueError as exc:
        raise FilterError(spec, f"lineno {text!r} is not an integer") from exc
    if lineno < 0:
        raise FilterError(spec, f"lineno {lineno} is negative")
    return lineno


@final
class _Collector:
    """One filter stack and the warnings it let through, aggregated so a repeat costs nothing.

    Locked: a test's `asyncio.to_thread(...)` can warn from a worker thread while the test's own
    task warns on the loop, and both share this collector through the copied context.
    """

    __slots__ = ("_counts", "_filters", "_limit", "_lock", "_needs_module")

    def __init__(
        self, filters: Sequence[WarningFilter], *, limit: int = MAX_DISTINCT_WARNINGS
    ) -> None:
        # Reversed once, here: the last filter to match wins, so a test's own filters (appended
        # after the session's) override them, and the last spec of a stacked mark overrides the
        # ones under it.
        self._filters = tuple(reversed(filters))
        # Deriving a module name off a filename is a `sys.modules` sweep; nothing pays for it
        # unless some filter actually narrows by module.
        self._needs_module = any(f.module is not None for f in self._filters)
        self._counts: dict[tuple[str, str, str, int], int] = {}
        self._limit = limit
        self._lock = threading.Lock()

    def handle(
        self, message: Warning | str, category: type[Warning], filename: str, lineno: int
    ) -> bool:
        """Record this warning if the stack lets it through, and return whether the stack says
        to raise it instead."""
        text = str(message)
        module = _module_name(filename) if self._needs_module else ""
        action = DEFAULT_ACTION
        for filter_ in self._filters:
            if filter_.matches(text, category, module, lineno):
                action = filter_.action
                break
        if action == "ignore":
            return False
        if action == "error":
            return True
        key = (category.__name__, text, filename, lineno)
        with self._lock:
            seen = self._counts.get(key)
            if seen is None:
                if len(self._counts) < self._limit:
                    self._counts[key] = 1
            elif action == "always":
                self._counts[key] = seen + 1
        return False

    def recorded(self) -> tuple[RecordedWarning, ...]:
        """What this collector let through, in the order each was first raised."""
        with self._lock:
            items = list(self._counts.items())
        return tuple(
            RecordedWarning(
                category=category, message=text, filename=filename, lineno=lineno, count=count
            )
            for (category, text, filename, lineno), count in items
        )


#: The collector of whichever test is running, or `None` outside one. A `ContextVar`, so the
#: executor thread a sync test's body runs in shares its test's collector (the context is copied
#: at submit time by `_capture.ContextPropagatingExecutor`) while a concurrently dispatched test
#: sees its own.
_collector: ContextVar[_Collector | None] = ContextVar("voci_warning_collector", default=None)

#: `warnings.catch_warnings()` entered by `install` and exited by `uninstall`: it saves and
#: restores `warnings.filters` and `warnings.showwarning` together, invalidating the per-module
#: warning registries on both ends the way hand-restoring the two lists would not.
_scope: warnings.catch_warnings | None = None
_previous_showwarning: Any = None
_session: _Collector | None = None
_session_filters: tuple[WarningFilter, ...] = ()
_swallow_hooks: list[SwallowHook] = []


def install(filters: Sequence[WarningFilter] = ()) -> bool:
    """Route every warning through this module's shim for the duration of one run, filtered by
    `filters`. Returns whether this call is the one that installed it -- `False` if an enclosing
    run already did, whose `uninstall` is then not this caller's to do.

    The process-wide filter list is set to `always` so that nothing is dropped before the shim
    sees it: what a warning is worth is decided here, per test, and CPython's own per-module
    registries would otherwise silence the second test to raise the same one.
    """
    global _scope, _previous_showwarning, _session, _session_filters
    if _scope is not None:
        return False
    scope = warnings.catch_warnings()
    scope.__enter__()
    _previous_showwarning = warnings.showwarning
    warnings.simplefilter("always")
    warnings.showwarning = _showwarning
    _session_filters = tuple(filters)
    _session = _Collector(_session_filters)
    _scope = scope
    return True


def uninstall() -> None:
    """Restore what `install` replaced, keeping the session's recorded warnings readable.
    Idempotent, and safe from a `finally`."""
    global _scope, _previous_showwarning
    scope, _scope = _scope, None
    _previous_showwarning = None
    if scope is not None:
        scope.__exit__(None, None, None)


def session_warnings() -> tuple[RecordedWarning, ...]:
    """What was warned about with no test running: during collection, a session fixture's
    teardown, or after the run. Empty until `install` has been called."""
    return () if _session is None else _session.recorded()


@contextmanager
def collecting(filters: Sequence[WarningFilter] = ()) -> Iterator[_Collector]:
    """Gather the warnings raised while this context is open, under `filters` layered over the
    session's. Warnings raised on a task or thread that doesn't inherit this context go to the
    session collector instead."""
    collector = _Collector((*_session_filters, *filters))
    token = _collector.set(collector)
    try:
        yield collector
    finally:
        _collector.reset(token)


def register_swallow(hook: SwallowHook) -> None:
    """Give `hook` first refusal on every warning, ahead of any filter."""
    _swallow_hooks.append(hook)


def unregister_swallow(hook: SwallowHook) -> None:
    """Undo one `register_swallow`. Silent about a hook that isn't registered."""
    with contextlib.suppress(ValueError):
        _swallow_hooks.remove(hook)


def _showwarning(
    message: Warning | str,
    category: type[Warning],
    filename: str,
    lineno: int,
    file: Any = None,
    line: str | None = None,
) -> None:
    """`warnings.showwarning`'s replacement for the run.

    Raises the warning itself under the `error` action, which propagates out of the
    `warnings.warn()` that raised it and fails whichever phase made that call.
    """
    for hook in _swallow_hooks:
        if hook(message, category, filename, lineno):
            return
    collector = _collector.get() or _session
    if collector is None:
        _previous_showwarning(message, category, filename, lineno, file, line)
        return
    if collector.handle(message, category, filename, lineno):
        raise message if isinstance(message, Warning) else category(message)


#: `{__file__: dotted module name}`, rebuilt whenever `sys.modules` has changed size -- what a
#: filter's `module` field is matched against, which `showwarning` is handed no other way.
_module_names: dict[str, str] = {}
_modules_seen = -1
_modules_lock = threading.Lock()


def _module_name(filename: str) -> str:
    """The dotted name of the module `filename` was imported as, or the filename with `.py`
    stripped when no imported module claims it -- CPython's own fallback for the same case."""
    global _modules_seen
    with _modules_lock:
        count = len(sys.modules)
        if count != _modules_seen:
            _module_names.clear()
            for name, module in list(sys.modules.items()):
                path = getattr(module, "__file__", None)
                if path:
                    _module_names.setdefault(path, name)
            _modules_seen = count
        return _module_names.get(filename) or filename.removesuffix(".py")
