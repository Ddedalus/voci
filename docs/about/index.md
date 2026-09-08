# About

voci is a test runner for Python codebases whose tests spend most of their time waiting. It runs
the whole suite in one process on one event loop, dispatching each test as a concurrent
`asyncio` task, and wires fixtures through parameter defaults rather than name lookup.

If you are here to use it, the [Guide](../guide/index.md) is the front door and the
[Reference](../reference/index.md) is what you will come back to.

voci is pre-release, ahead of v0.1, and the public API is not frozen.
