# Assertions

A plain `assert` needs nothing from voci to work, but voci vendors pytest's assertion rewriter, so
a failure explains itself instead of just saying `False`:

```python
async def test_creates_a_user(client: Annotated[AsyncClient, Depends(api_client)]) -> None:
    response = await client.post("/users", json={"email": "erin@example.com"})
    assert response.status_code == 200
```

```console
>       assert response.status_code == 200
E       assert 201 == 200
```

The rewrite happens at import time, for modules under a configured test root — a file imported
from application code keeps a plain `assert`, which raises a bare `AssertionError` with no
explanation. voci underlines the failing expression from the traceback's own column info in that
case; recovering the operands' actual values needs the rewrite.

## Catching an expected exception

`voci.raises` is a context manager: enter it around code that should raise, and its
`ExceptionInfo` is populated once the block exits.

```python
with voci.raises(ValueError, match="insufficient balance"):
    await svc.transfer(source, target, 500)
```

`match` is an `re.search` against `str(exception)`, so a plain substring works and regex
metacharacters in an otherwise literal message need escaping. Given a callable as a second
positional argument instead of a `with` block, `raises` calls it and returns the
`ExceptionInfo` directly: `voci.raises(ValueError, func, *args)`.

`ExceptionInfo.value` is the caught exception, `.type` its class, `.traceback` its traceback;
reading `.value` before the block has exited raises `RuntimeError`, not a silent `None`. Naming an
`expected` broad enough to catch `asyncio.CancelledError` raises `TypeError` instead of silently
swallowing it — a `with voci.raises(BaseException):` block around a slow call would otherwise
absorb the cancellation voci uses to enforce `@voci.timeout`, and no test using it could ever be
timed out again.

## Comparing floats

`voci.approx` wraps a number, or a list, tuple or dict of numbers, as a value that compares equal
to anything within tolerance:

```python
assert orders[0]["total_cents"] / 100 == voci.approx(19.99)
```

The defaults are `rel=1e-6` and `abs=1e-12`, whichever tolerance is looser; naming `abs` alone
drops the relative default so only the absolute tolerance applies, and naming `rel` alone keeps
the absolute default underneath it, which is what lets a comparison against zero work at all. A
list or tuple compares elementwise by position, a dict elementwise by key, both under the same
tolerances; nesting a container inside one, or comparing a set, raises `TypeError` rather than
comparing something `approx` didn't actually check. Two `NaN`s compare equal only with
`nan_ok=True`.
