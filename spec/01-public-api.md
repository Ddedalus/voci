# 01 — Public API

Status: initial human review.

The surface a test author writes against and what is hardest to change.

## 1. Design rules

1. **Everything is explicit and statically visible.** 
2. **Fixtures are values, not names.** 
3. **Cheap to introspect.** 
4. **Small.** One decorator for fixtures, one function for injection, a handful of marks.

## 2. Tests

A test is a module-level (or class-level, see §7) function whose name starts with `test_`, in a file
matching the discovery patterns ([03](03-discovery-and-collection.md)). Only `async def` tests are supported in MVP.

```python
async def test_health(client: AsyncClient = Depends(api_client)):
    r = await client.get("/health")
    assert r.status_code == 200
```

An un-awaited coroutine warning raised during a test fails the test (R§5, invariant I8).

## 3. Fixtures

The fixture decorator always requires parentheses, even with no arguments.

```python
import velox
from velox import Depends


@velox.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    e = create_async_engine(DB_URL)
    try:
        yield e
    finally:
        await e.dispose()
```

The wiring syntax is **exactly FastAPI's**: `param: Type = Depends(dependency)`.

**What it takes is an already-analyzed object, not a raw callable.** FastAPI accepts any callable and introspects it later, at route registration, with `inspect.signature` +
   `get_type_hints`. `@velox.fixture()` has already done that work at decoration time: a `Fixture`
   arrives carrying its scope, its exclusivity token, and its own resolved dependency plan.

### What `@velox.fixture()` accepts

| Argument | Default | Meaning |
|---|---|---|
| `scope` | `"function"` | `"call"` \| `"function"` \| `"module"` \| `"session"`. See [04](04-dependency-injection.md). |
| `exclusive` | `False` | `True`, or a string token. Tests transitively depending on it never run concurrently with each other ([06](06-scheduling-and-determinism.md)). |
| `name` | `fn.__name__` | Display name in errors, reports, and `--durations`. |


Fixtures may be async functions, or async generators. Generators
yield exactly once; everything after the `yield` is teardown, and teardown runs inside the fixture's own `try/finally`

### `Depends()`

Depends shall apply the same typing hack that FastAPI uses, but with better typing. Type as returning the return typ of the fixture, but return a sentinel captured by the DI resolver. `session: AsyncSession = Depends(session)` is a type error if the fixture yields something else.

The `@velox.fixture` decorator unwraps the generator/awaitable and returns a `Fixture[T]`;

An injected parameter must have `Depends(...)` as its default. A parameter with any other default is a plain Python default and is left alone (this is how `@parametrize` and closures over ordinary
values keep working).

The annotation is encouraged, but not required: `client = Depends(api_client)` works.

### The FastAPI client fixture

`api_client` is the fixture almost every suite in the reference stack writes first, so it is the
canonical shape:

```python
from app.main import app  # the real, module-level app — untouched

@velox.fixture()
async def api_client(
    session: AsyncSession = Depends(session),
    settings: Settings = Depends(settings),
) -> AsyncIterator[AsyncClient]:
    async with velox.fastapi.client(
        app,
        overrides={get_session: lambda: session},
        state={"settings": settings},
    ) as client:
        yield client
```

**Production code is not restructured to be testable.** The app stays the module-level singleton it
already is; no `create_app()` factory, no import-time indirection, nothing in `app/` changes.
`velox.fastapi.client` replaces `app.dependency_overrides` (and, when `state=` is passed, `app.state`)
exactly once per app object with a layered proxy whose reads consult a `ContextVar` layer before the
underlying dict — so the override *view* is per-test even though the app object is shared, and two
tests overriding `get_session` with different sessions at the same time cannot see each other's
values. Overrides keep FastAPI's own semantics: the value is a dependency *callable*, not the
resolved object. Mechanism, escalation, lifespan policy, and limits: [08](08-patching-and-isolation.md) §3.1.

### Direct call / overriding

A fixture object is also callable in ordinary Python (`await engine()` returns the underlying
async generator context) — but the supported override mechanism is **passing a different value at the call site**, which is what makes DI-based mocking tier (a) work:

```python
@velox.parametrize("clock", [FrozenClock(...), RealClock()])
async def test_expiry(clock: Clock, svc: Service = Depends(service)): ...
```

and, for replacing one node in a graph for one test:

```python
async def test_retry(svc: Service = Depends(service.with_(http=fake_http))): ...
```

`Fixture.with_(**overrides)` returns a derived fixture whose named dependencies are replaced.
Overrides are part of the static graph, so scheduling and validation still see the truth.

> Review note: this construct seems weird, do not implement without further discussion.


## 4. Marks

Marks are decorators, not string names, and they attach a small frozen record to the function.

```python
@velox.skip("not implemented yet")
@velox.skipif(sys.platform == "win32", reason="posix only")
@velox.xfail("flaky upstream", strict=True, raises=TimeoutError)
@velox.tag("slow", "integration")          # selected with -m
@velox.timeout(30)                          # seconds; overrides --timeout
@velox.solo                                 # runs alone, whole suite drained
@velox.isolated                             # runs in a subprocess on a fresh loop
@velox.parametrize("n,expected", [(1, 2), (2, 4)], ids=...)
```

### Parametrize

```python
@velox.parametrize("payload", [{"a": 1}, {"a": 2}], ids=["one", "two"])
@velox.parametrize("verb", ["GET", "POST"])
async def test_endpoint(verb: str, payload: dict, client: AsyncClient = Depends(api_client)): ...
```

Stacked decorators produce the cartesian product in a **stable, defined order** (outermost varies
slowest), with generated ids from `repr`-based rules identical to pytest's for the common types
(str/int/bool/None/enum → literal; everything else → `argname0`, `argname1`, …). No `set` iteration
and no unpinned `hash()` anywhere in id generation (R§6). `indirect=` is not supported — the DI
equivalent is a parametrized value passed into a fixture via `with_`.

## 5. Test IDs

`relative/path/to/test_file.py::test_name[param-id]`, with `::ClassName::` for class-grouped tests.
Computed directly from the file path and `__qualname__` — no parent-chain derivation (R§3). Kept
byte-identical to pytest's addressing syntax so that `--deselect`, copy-pasted CI selectors, and
editor integrations transfer unchanged.

## 6. Built-in fixtures

| Fixture | Notes |
|---|---|
| `velox.tmp_path` | `Path`, unique per test by construction: `basetemp/<sanitized-test-id>` (no scan-and-retry; that is a serial-era artifact — R§7). |
| `velox.tmp_path_factory` | Session-scoped; same numbered-root + retention policy as pytest. |
| `velox.capture` | Access to the current test's captured stdout/stderr text ([09](09-capture-and-logging.md)). |
| `velox.log_records` | The `caplog` equivalent: structured `LogRecord`s captured for this test, plus a `set_level()` context manager. |
| `velox.test_info` | Test id, tags, timeout, worker slot — the `request` replacement, deliberately tiny and read-only. |
| `velox.fastapi.lifespan` | Session-scoped; runs one app's lifespan exactly once per run, since the test client never triggers startup. Writes land in the app's base state ([08](08-patching-and-isolation.md) §3.1). |
| `velox.monkeypatch` | *Not provided.* Use `unittest.mock` (which velox schedules solo) or a DI override — see [08](08-patching-and-isolation.md). |

## 7. Class-grouped tests

Supported as pure namespacing: a class whose name starts with `Test`, with no `__init__`, whose
`test_*` methods are collected with `self` ignored (velox instantiates once per test, no state shared through the instance). No `setup_method`/`teardown_method` — that is what a function-scoped fixture is. Class scope for fixtures is not in the MVP (Q2).

## 8. Assertions

Plain `assert`. The vendored rewriter provides pytest-quality introspection in test modules, and a PEP 657 caret fallback covers everything else ([07](07-assertions.md)).

```python
with velox.raises(ValueError, match="bad input"):
    ...
velox.approx(0.3)
```

`__tracebackhide__ = True` is honored in user helper functions (R§7).

## 9. MVP

- `@velox.fixture(scope=, exclusive=, name=)`, `Depends()`, sync/async, generator teardown.
- `skip`, `skipif`, `tag`, `timeout`, `parametrize`, `solo`.
- `raises`, `approx`, plain asserts.
- Class grouping as namespacing.
- `velox.fastapi.client(app, overrides=, state=, base_url=)` and `velox.fastapi.lifespan(app)` —
  the layered-override client for the reference stack ([08](08-patching-and-isolation.md) §3.1).

## 10. Roadmap

- `tmp_path`, `tmp_path_factory`, `capture`, `log_records`, `test_info`.
- `Fixture.with_()` for direct-dependency override.
- `@velox.isolated` (needs the subprocess tier, [08](08-patching-and-isolation.md)).
- `Annotated[T, Depends(fixture)]` as a second accepted form — FastAPI now recommends `Annotated`
  over the default-value form, so migrating codebases will expect it. It requires evaluating
  annotations, so it is resolved once per callable and cached, and the default-value form stays the
  documented default (Q1).
- Parametrized fixtures (`@velox.fixture(params=[...])`) with per-param scope instances — the
  cache-key machinery in [04](04-dependency-injection.md) is already designed for it.
- `class` scope; deep `with_()` override by dependency path.
- `velox.approx` for nested structures; richer `raises` (`ExceptionGroup` matching, `.group_contains`).

## 11. Open questions

- **Q7** — Is `tag` + `-m` worth having at all? Yes.
