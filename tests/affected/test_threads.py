"""Tests for voci._affected.threads: the opt-in `affected_trace_threads` patches."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from collections.abc import Iterator

import pytest

from voci._affected import threads

_var: contextvars.ContextVar[str] = contextvars.ContextVar("test_threads_var", default="outer")


def _add(a: int, b: int) -> int:
    """Module-level, not a lambda: `ProcessPoolExecutor` needs to pickle this."""
    return a + b


@pytest.fixture
def installed() -> Iterator[None]:
    assert threads.install()
    try:
        yield
    finally:
        threads.uninstall()


def test_install_returns_false_when_already_installed(installed: None) -> None:
    assert threads.install() is False


def test_uninstall_is_a_no_op_when_never_installed() -> None:
    threads.uninstall()  # must not raise


def test_thread_start_does_not_copy_context_without_the_patch() -> None:
    """The baseline this patch changes: unpatched, a bare `Thread` gets none of its creator's
    context, matching the plan's "neither 3.13 nor 3.14 passes context to new threads"."""
    seen = []
    token = _var.set("caller-set")
    try:
        t = threading.Thread(target=lambda: seen.append(_var.get()))
        t.start()
        t.join()
    finally:
        _var.reset(token)
    assert seen == ["outer"]


def test_installed_thread_start_copies_the_callers_context(installed: None) -> None:
    seen = []
    token = _var.set("caller-set")
    try:
        t = threading.Thread(target=lambda: seen.append(_var.get()))
        t.start()
        t.join()
    finally:
        _var.reset(token)
    assert seen == ["caller-set"]


def test_installed_thread_start_captures_context_at_start_not_construction(installed: None) -> None:
    """The creator's context is whichever one is active when `.start()` runs, not whichever was
    active when the `Thread` object was built -- the two differ whenever construction and
    `.start()` happen on different tasks, or the same task's context changed in between."""
    seen = []
    t = threading.Thread(target=lambda: seen.append(_var.get()))
    token = _var.set("set-after-construction")
    try:
        t.start()
        t.join()
    finally:
        _var.reset(token)
    assert seen == ["set-after-construction"]


def test_uninstall_restores_the_original_thread_start(installed: None) -> None:
    original = threading.Thread.start
    threads.uninstall()
    assert threading.Thread.start is not original  # patched one is still active until reassigned
    seen = []
    token = _var.set("caller-set")
    try:
        t = threading.Thread(target=lambda: seen.append(_var.get()))
        t.start()
        t.join()
    finally:
        _var.reset(token)
    assert seen == ["outer"]


def test_run_in_executor_does_not_copy_context_without_the_patch() -> None:
    async def body() -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _var.get)

    token = _var.set("caller-set")
    try:
        result = asyncio.run(body())
    finally:
        _var.reset(token)
    assert result == "outer"


def test_installed_run_in_executor_copies_the_callers_context(installed: None) -> None:
    async def body() -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _var.get)

    token = _var.set("caller-set")
    try:
        result = asyncio.run(body())
    finally:
        _var.reset(token)
    assert result == "caller-set"


def test_installed_run_in_executor_passes_positional_args_through(installed: None) -> None:
    async def body() -> int:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda a, b: a + b, 1, 2)

    assert asyncio.run(body()) == 3


def test_installed_run_in_executor_captures_context_per_call_on_a_shared_pool(
    installed: None,
) -> None:
    """Unlike `Thread.start`, the same worker thread is reused across calls, so the context has
    to travel with each submission rather than living on the thread (see the plan's Async
    offload bullet)."""
    from concurrent.futures import ThreadPoolExecutor

    async def body(pool: ThreadPoolExecutor) -> tuple[str, str]:
        loop = asyncio.get_running_loop()
        token = _var.set("first-caller")
        try:
            first = await loop.run_in_executor(pool, _var.get)
        finally:
            _var.reset(token)
        token = _var.set("second-caller")
        try:
            second = await loop.run_in_executor(pool, _var.get)
        finally:
            _var.reset(token)
        return first, second

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = asyncio.run(body(pool))
    assert result == ("first-caller", "second-caller")


def test_installed_run_in_executor_leaves_a_process_pool_untouched(installed: None) -> None:
    """A `ProcessPoolExecutor` call item is pickled to the worker process, and a
    `contextvars.Context` can't be pickled at all -- wrapping `func` for one would turn a call
    that worked before this patch into a submission-time `TypeError` (see the plan's Child
    processes section: a separate process needs `affected_trace_subprocesses`, not this)."""
    from concurrent.futures import ProcessPoolExecutor

    async def body(pool: ProcessPoolExecutor) -> int:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(pool, _add, 1, 2)

    with ProcessPoolExecutor(max_workers=1) as pool:
        result = asyncio.run(body(pool))
    assert result == 3
