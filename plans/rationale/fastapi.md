# `fastapi.py` — per-test dependency overrides

See [rationale.md](../rationale.md) for the index.

**The app stays a singleton; the *view* of it becomes per-test.** `dependency_overrides` and
`state` are plain per-app-instance data, so concurrent tests against one module-level `app` all
write the same dict — and the documented teardown idiom, `clear()`, wipes everyone else's overrides
too. The alternative usually recommended is an app factory, which is a change to production code
made solely for tests.

velox swaps each attribute once, per app, for a proxy layering a `ContextVar` of per-test values
over what was already there. Three upstream facts make it work, all verified by reading the source
rather than assumed: a route stores only a *pointer* to the app and resolves the override at
request-solve time via `.get(call, call)`; `request.app` comes from the ASGI scope, which Starlette
repopulates from the singleton on every request, so installing the proxy after routes exist still
catches everything; and `ASGITransport` awaits the app in the calling task, so a request inherits
the test's context. Swapping in a per-test app instance instead would break the first fact — routes
point at *this* app, so a copy is a different app, not an isolated view of the same one.
