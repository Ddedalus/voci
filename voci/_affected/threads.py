"""Opt-in `[tool.voci] affected_trace_threads` (`plans/affected-tests-plan.md`, Tracer's
"Threads" and "Async offload" bullets): for the duration of a run, `install()` patches
`Thread.start` and `loop.run_in_executor` so code that ends up running on a thread inherits
whichever collector was current in the thread or task that created it, instead of running with
none -- which `collector.record_first_party` would otherwise read as "one of the tests in flight
did something I can't attribute", distrusting all of them.

Off by default: either patch is something a test could in principle observe (a `contextvars.Context`
copy is not free, and both replace a stdlib entry point for the run's duration), which is why this
module is never imported except behind the config key -- see the plan's "No surprise patching by
default". Not yet wired to a real run (`cli._installed_session` doesn't call this yet, the same way
nothing yet starts a `Tracer`; see `_affected/__init__.py`).

One implementation covers 3.13 and 3.14 alike, even though 3.14 alone added a native
`Thread(context=...)` constructor parameter for this: wrapping `self.run` in a context-replaying
closure -- what 3.13 needs regardless, having no such parameter at all -- captures the calling
context at `start()` time on both versions identically, without depending on `Thread`'s private
`_context` attribute (probed on both interpreters).
"""

from __future__ import annotations

import asyncio.base_events
import contextvars
import functools
import threading
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from typing import Any

__all__ = ["install", "uninstall"]

type _RunInExecutor = Callable[..., asyncio.Future[Any]]

#: `None` when not installed; the original bound-or-unbound callable to restore otherwise. Module
#: globals, not a class, because there is exactly one of each to patch process-wide -- same shape
#: as `_rewrite`'s and `_warnings`' own `install`/`uninstall` pair, which `cli._installed_session`
#: will eventually call this alongside.
_original_thread_start: Callable[[threading.Thread], None] | None = None
_original_run_in_executor: _RunInExecutor | None = None


def install() -> bool:
    """Patch `Thread.start` and `loop.run_in_executor` for the duration of a run. Returns
    whether *this* call installed them, `False` if an enclosing call already had -- the same
    idempotence `_rewrite.install`/`_warnings.install` give a nested `main()` call, so only the
    outermost call's `uninstall()` tears anything down.
    """
    global _original_thread_start, _original_run_in_executor
    if _original_thread_start is not None:
        return False
    _original_thread_start = threading.Thread.start
    _original_run_in_executor = asyncio.base_events.BaseEventLoop.run_in_executor
    threading.Thread.start = _traced_start  # type: ignore[method-assign]
    asyncio.base_events.BaseEventLoop.run_in_executor = _traced_run_in_executor  # type: ignore[method-assign]
    return True


def uninstall() -> None:
    """Undo `install()`. A no-op if it was never called, so a caller can always run this in a
    `finally` without checking whether `install()` succeeded, or ran at all, first."""
    global _original_thread_start, _original_run_in_executor
    if _original_thread_start is not None:
        threading.Thread.start = _original_thread_start  # type: ignore[method-assign]
        _original_thread_start = None
    if _original_run_in_executor is not None:
        asyncio.base_events.BaseEventLoop.run_in_executor = _original_run_in_executor  # type: ignore[method-assign]
        _original_run_in_executor = None


def _traced_start(self: threading.Thread) -> None:
    """Capture the calling context now, at `start()` time -- the creator's context, whether that
    creator is the thread that built `self` or a different one that only called `start()` on it
    -- and make `self.run` replay it. `functools.wraps` keeps `self.run`'s `__name__` and
    friends intact for anything downstream that introspects it (a debugger, a repr)."""
    assert _original_thread_start is not None  # install() always sets this before patching
    ctx = contextvars.copy_context()
    original_run = self.run

    @functools.wraps(original_run)
    def run_in_context() -> None:
        ctx.run(original_run)

    self.run = run_in_context  # type: ignore[method-assign]
    _original_thread_start(self)


def _traced_run_in_executor(
    self: asyncio.AbstractEventLoop,
    executor: Executor | None,
    func: Callable[..., object],
    *args: object,
) -> asyncio.Future[Any]:
    """Copy the calling task's context into `func` per call, not per worker thread: unlike a
    thread `Thread.start` creates fresh, a `ThreadPoolExecutor`'s workers are long-lived and
    shared across whichever unrelated tests submit to them next, so the context has to travel
    with each submission instead of living on the thread.

    Left untouched for anything but a thread pool (`executor is None`, `run_in_executor`'s own
    default, or an explicit `ThreadPoolExecutor`): a `ProcessPoolExecutor` call item is pickled
    to the worker process, and `functools.partial(ctx.run, ...)` would carry a
    `contextvars.Context` into that pickle, which can't be pickled at all -- turning a call that
    worked before this patch into a `TypeError` at submission time. A process pool shares no
    memory with this one regardless, so there is no context for it to inherit in the first
    place; tracing a same-interpreter child process is `affected_trace_subprocesses`'s job
    (M6), not this one's.
    """
    assert _original_run_in_executor is not None  # install() always sets this before patching
    if executor is not None and not isinstance(executor, ThreadPoolExecutor):
        return _original_run_in_executor(self, executor, func, *args)
    ctx = contextvars.copy_context()
    return _original_run_in_executor(self, executor, functools.partial(ctx.run, func, *args))
