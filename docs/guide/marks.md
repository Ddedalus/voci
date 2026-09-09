# Marks

A mark is a decorator that attaches a fixed record to a test function: `@voci.skip(...)`,
`@voci.tag(...)`, and the others in [Marks](../reference/marks.md) all work this way. They stack
freely and in any order, with one exception below — `@voci.parametrize` is covered on its own
page.

## Skipping

`@voci.skip` always skips, with a reason that shows up in the run's report:

```python
@voci.skip("pagination is not implemented yet (GET /users has no route -- 405, not 200)")
async def test_list_users_is_paginated(
    client: Annotated[AsyncClient, Depends(api_client)],
) -> None:
    response = await client.get("/users?limit=10")
    assert response.status_code == 200
```

`@voci.skipif` takes a condition instead, decided once at collection — a `bool`, or a
zero-argument callable for one that shouldn't be evaluated at import time:

```python
@voci.skipif(not REQUEST_ID_MIDDLEWARE_ENABLED, reason="middleware is behind a feature flag")
async def test_response_carries_request_id(
    client: Annotated[AsyncClient, Depends(api_client)],
) -> None: ...
```

`@voci.skipif` stacks — a test with several is skipped if any one condition holds.

## Expected failures

`@voci.xfail` marks a failure as expected without skipping the test — it still runs, and a call
phase that raises reports `XFAILED`:

```python
@voci.xfail("cache does not evict on size yet", raises=AssertionError)
async def test_evicts_when_full(c: Annotated[TTLCache, Depends(cache)]) -> None:
    for i in range(10_000):
        c.put(f"k{i}", b"v")
    assert len(c._entries) <= 1_000
```

`raises` narrows which exception type counts as the expected one — anything else still reports
`FAILED`. A test that passes despite `@voci.xfail` reports `XPASSED`; add `strict=True` to make
that count as a failure instead.

## Tags and selection

`@voci.tag` attaches string labels with no fixed meaning of their own — you choose what a tag
means for your suite:

```python
@voci.tag("slow")
async def test_bulk_signup(client: Annotated[AsyncClient, Depends(api_client)]) -> None: ...
```

`voci -m 'slow'` runs only tagged tests; `-m 'not slow'` excludes them. A tag stacks like
`skipif` — a test can carry several — and a test whose tags don't satisfy `-m`'s expression is
deselected, not skipped, so it never shows up as `SKIPPED` in the report. A `@voci.skip`-marked
test is skipped regardless of `-m`.

## Stacking and duplicates

Every mark but `@voci.parametrize` folds into the same per-function record regardless of order, so
`@voci.tag("slow")` above or below `@voci.skip(...)` means the same thing either way.
`@voci.skip`, `@voci.xfail` and `@voci.timeout` carry one fixed value each rather than a set:
applying any of them twice to the same test raises `TypeError` naming the function and the value
already set, instead of overwriting the first one silently.

[Marks](../reference/marks.md) covers the full set, including `@voci.timeout`, `@voci.solo` and
`@voci.isolated` for concurrency control, and `@voci.parametrize`/`@voci.case` for per-case marks.
