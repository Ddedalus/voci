"""Does a ContextVar set by the "test" survive into an async-offload worker thread, per call,
even when the same worker thread is reused across two different "tests"?

This is what decides whether the audit hook (data:/dir: dependencies) and the sys.monitoring
tracer correctly attribute a file read or DB call made through `asyncio.to_thread`,
`loop.run_in_executor`, or `anyio.to_thread.run_sync` -- the primitives async file/DB libraries
(aiofiles, anyio.Path, aiosqlite) build on for what is, for an async-first app, an ordinary file
or database read.

    uv run python research/affected/probes/async_file_io_context.py
"""

from __future__ import annotations

import asyncio
import contextvars

current_test: contextvars.ContextVar[str] = contextvars.ContextVar("current_test", default="<none>")


def read_current_test() -> str:
    """Stand-in for the sys.monitoring / audit-hook callback: reads whichever collector is
    "current" in whatever thread this happens to run on."""
    return current_test.get()


async def probe(label: str, call) -> None:
    results = []
    for name in ("test_a", "test_b", "test_a"):
        current_test.set(name)
        results.append(await call())
    correct = results == ["test_a", "test_b", "test_a"]
    print(f"{label:<28} {results}  {'OK' if correct else 'WRONG -- context did not follow the call'}")


async def main() -> None:
    loop = asyncio.get_running_loop()

    await probe("asyncio.to_thread", lambda: asyncio.to_thread(read_current_test))
    await probe("run_in_executor (raw)", lambda: loop.run_in_executor(None, read_current_test))

    import anyio

    await probe("anyio.to_thread.run_sync", lambda: anyio.to_thread.run_sync(read_current_test))


if __name__ == "__main__":
    asyncio.run(main())
