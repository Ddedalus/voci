# Parametrize

`@voci.parametrize` runs one test function against several cases, each collected and reported as
its own test:

```python
@voci.parametrize("status_code", [200, 404, 409])
async def test_returns_json(
    status_code: int, client: Annotated[AsyncClient, Depends(api_client)]
) -> None:
    response = await client.get(f"/probe/{status_code}")
    assert response.json()["status_code"] == status_code
```

`argnames` is a name or a comma-separated string of names (`"status_code"`, or `"n,expected"` for
several); `argvalues` is one entry per case — the value itself for a single name, a tuple aligned
to the names for several. Each case reaches the test as a keyword argument matched by name, so it
can sit anywhere in the signature, including after a fixture parameter with no default. A case's
display id comes from its value (`test_returns_json[200]`, `[404]`, `[409]`) unless `ids=` gives
one id per case, or a callable that turns a value into one.

## Marking one case

`voci.case(...)` wraps a single case to carry marks for that case alone, rather than the whole
test:

```python
@voci.parametrize("status_code", [200, 404, voci.case(409, marks=voci.xfail("not implemented"))])
async def test_returns_json(
    status_code: int, client: Annotated[AsyncClient, Depends(api_client)]
) -> None: ...
```

`marks=` takes one mark decorator or a sequence of them, meaning for that case what they'd mean
written above the `def` — the case's marks and the test's own fold together, with the case's
winning wherever only one of `skip`, `xfail`, or `timeout` can stand. `case(...)` doesn't set a
custom id; use `ids=` on `parametrize` for that.

## Stacking

Several `@voci.parametrize` decorators on one test combine into the cartesian product of their
cases — every value of one against every value of the other — with the outermost decorator varying
slowest:

```python
@voci.parametrize("role", ["admin", "guest"])
@voci.parametrize("locale", ["en", "fr"])
async def test_renders(role: str, locale: str) -> None: ...
```

produces four tests, `role` changing once for every two of `locale`. Two stacked decorators can't
declare the same argument name — that raises at collection.

## Parametrizing a fixture

`@voci.fixture(params=[...])` multiplies every test that transitively depends on it, one collected
test per value, the same way `@voci.parametrize` multiplies a test over its own arguments. The
fixture function receives each case through a parameter named `param`:

```python
@voci.fixture(params=["sqlite", "postgres"])
def dialect(param: str) -> str:
    return param
```

`ids=` works the same way here as on `parametrize` — a same-length sequence of strings, or a
callable — and is only valid alongside `params=`.

The full example is `examples/01-fastapi-crud/tests/test_users.py::test_create_user`, which
parametrizes over three email addresses, each becoming its own reported test
(`test_create_user[bob]`, `[tag-in-local-part]`, `[subdomain]`) — see that example's README for the
rest of the suite it sits in.
