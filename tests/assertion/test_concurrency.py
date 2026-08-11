"""Tests that the vendored assertion rewriter's per-test state -- `util._reprcompare`,
`util._assertion_pass`, `util._config` -- is carried in ContextVars, isolated across
concurrent asyncio tasks and threads.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import cast

from velox._assertion_state import CONTEXT_GLOBALS, assertion_state, get_config
from velox._rewrite import Config, assertion_context
from velox._vendor.assertion import util


def test_vendored_util_reads_through_the_contextvars() -> None:
    """The vendored rewriter says `util._reprcompare`; that must reach velox's ContextVar."""
    assert "_reprcompare" in CONTEXT_GLOBALS
    assert util._reprcompare is None

    def hook(op: str, left: object, right: object) -> str | None:
        return "explained"

    with assertion_state(reprcompare=hook):
        assert util._reprcompare is hook
    assert util._reprcompare is None


def test_the_globals_are_not_module_attributes() -> None:
    for name in CONTEXT_GLOBALS:
        assert name not in vars(util), f"{name} is a module global again"


def test_unknown_attribute_still_raises_attribute_error() -> None:
    try:
        util._not_a_real_thing  # noqa: B018
    except AttributeError as exc:
        assert "_not_a_real_thing" in str(exc)
    else:
        raise AssertionError("expected AttributeError")


def test_state_restores_on_exit() -> None:
    outer = Config(verbosity=1)
    with assertion_state(config=outer):
        assert get_config() is outer
        with assertion_state(config=Config(verbosity=2)):
            inner = get_config()
            assert inner is not None
            assert inner.get_verbosity() == 2
        assert get_config() is outer
    assert get_config() is None


def test_concurrent_tasks_do_not_see_each_others_config() -> None:
    """Overlapping tasks, each with a distinct verbosity, each see only their own assertion
    state."""
    task_count = 8

    async def one(verbosity: int) -> int:
        config = Config(verbosity=verbosity)
        with assertion_context(config):
            # Give every sibling a chance to overwrite the state before reading it back.
            for _ in range(3):
                await asyncio.sleep(0)
            observed = get_config()
            assert observed is not None
            return observed.get_verbosity()

    async def main() -> list[int]:
        return await asyncio.gather(*(one(i) for i in range(task_count)))

    assert asyncio.run(main()) == list(range(task_count))


def test_concurrent_tasks_get_their_own_reprcompare() -> None:
    """The explanation hook is per-test too, not shared global state."""

    async def one(tag: str) -> str | None:
        with assertion_state(reprcompare=lambda op, left, right: tag):
            for _ in range(3):
                await asyncio.sleep(0)
            # Read through the vendored module's `__getattr__`, exactly as the rewriter does.
            hook = cast(Callable[[str, object, object], str | None] | None, util._reprcompare)
            assert hook is not None
            return hook("==", 1, 2)

    async def main() -> list[str | None]:
        return await asyncio.gather(*(one(f"tag-{i}") for i in range(6)))

    assert asyncio.run(main()) == [f"tag-{i}" for i in range(6)]


def test_threads_get_their_own_context() -> None:
    """ContextVars are per-thread as well as per-task."""
    seen: dict[int, int] = {}
    barrier = threading.Barrier(4)

    def run(index: int) -> None:
        with assertion_state(config=Config(verbosity=index)):
            barrier.wait()
            config = get_config()
            assert config is not None
            seen[index] = config.get_verbosity()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert seen == {i: i for i in range(4)}


def test_explanation_is_produced_under_concurrency(rewritten) -> None:
    """End to end: concurrent failing asserts each get their own correct explanation."""
    mod = rewritten(
        """
        import asyncio

        async def check(n):
            await asyncio.sleep(0)
            assert [n] == [n + 1]
        """
    )

    async def one(n: int) -> str:
        with assertion_context(Config()):
            try:
                await mod.check(n)
            except AssertionError as exc:
                return str(exc)
        raise AssertionError("should have failed")

    async def main() -> list[str]:
        return await asyncio.gather(*(one(n) for n in range(6)))

    messages = asyncio.run(main())
    for n, message in enumerate(messages):
        assert f"assert [{n}] == [{n + 1}]" in message
        assert f"At index 0 diff: {n} != {n + 1}" in message
