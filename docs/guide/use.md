# Use

Some fixtures exist for their side effect, not their value — resetting a database between tests,
say — and no test needs to name one just to get that effect. `voci.use(...)` declares such a
fixture on behalf of every test in a module, or a whole package, instead:

```python
@voci.fixture()
def db_reset() -> Iterator[None]:
    truncate_all_tables()
    yield
    truncate_all_tables()


# test_users.py
import voci

from tests.fixtures import db_reset

voci.use(db_reset)


async def test_create_user(client: Annotated[AsyncClient, Depends(api_client)]) -> None: ...
```

Every test in `test_users.py` now builds and tears down `db_reset` without naming it. The fixture
is the same object a `Depends(...)` site would name — an imported, `@voci.fixture()`-decorated
function — and `voci.use()` never returns its value to a test; it declares the dependency and
discards it.

`voci.use(...)` is a statement, and only legal in a module body: a test module, or a package's
`__init__.py`. Calling it from inside a function raises `TypeError` — by the time that call ran,
collection would already be over. Passing something that isn't a fixture — `db_reset()`, called,
instead of `db_reset` itself — raises `TypeError` too.

A declared fixture builds before the test's own `Depends(...)` fixtures and tears down after them,
so a `db_reset` declared with `use()` is in place for the whole test, dependencies included.

## Package-wide declarations

Calling `voci.use(...)` in a package's `__init__.py` reaches every test in that directory and
below, not just the modules that import from it directly. A test can end up with fixtures declared
at more than one level — its own module, and one or more enclosing packages — and these accumulate
rather than override each other: several `use()` calls in one module apply in the order they're
written, and a package's declarations apply ahead of the module's own.

[Fixtures](../reference/fixtures.md) covers `use` alongside `fixture` and `Depends`.
