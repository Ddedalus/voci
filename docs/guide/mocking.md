# Mocking

`mock.patch`, and its `mock.patch.object`, `mock.patch.dict`, and `mock.patch.multiple`
variants, install by writing to a module or class attribute directly, so every test running at
the same time sees the patched value, not just the one that asked for it. voci treats the two
ways they get used — decorator and context manager — differently.

## Patching as a decorator

`@mock.patch` above a test function is visible on the function object at collection time, so
voci finds it there and schedules that test to run alone:

```python
@mock.patch("app.billing.gateway.charge")
async def test_checkout_charges_the_card(
    charge: mock.MagicMock,
    client: Annotated[AsyncClient, Depends(api_client)],
) -> None:
    charge.return_value = {"status": "succeeded"}
    response = await client.post("/checkout", json={"amount": 4200})
    assert response.status_code == 200
    charge.assert_called_once()
```

`charge` arrives first and positionally, the way `mock.patch` injects it under any runner;
`client` after it is resolved by voci as usual. `mock.patch.multiple` names its replacement by
keyword instead of position, and is resolved the same way. This test runs alone with no
`@voci.solo` written anywhere — the decorator is enough for voci to schedule it that way itself.

## Patching as a context manager

`with mock.patch(...):` inside a test body can't be seen ahead of time — nothing shows up on the
function object until the `with` block actually runs — so voci catches it at the point it tries
to install instead. A test that isn't running alone and enters one of these context managers
raises `GlobalPatchError`:

```python
async def test_checkout_charges_the_card(
    client: Annotated[AsyncClient, Depends(api_client)],
) -> None:
    with mock.patch("app.billing.gateway.charge", return_value={"status": "succeeded"}):
        response = await client.post("/checkout", json={"amount": 4200})

    assert response.status_code == 200
```

```
app.billing.gateway.charge was patched by a test that isn't running alone.

unittest.mock installs a patch by writing to the module or class itself, so every
test running at the same time sees it. Mark this test @voci.solo to run it alone,
or @voci.isolated to run it in its own subprocess. A patch applied as a decorator
is found at collection and scheduled alone for you.
```

The error is raised at the `with` line, before the patch reaches `app.billing.gateway` — so it
never gets written where another running test could read it. Marking the test `@voci.solo` runs
it alone and fixes it:

```python
@voci.solo
async def test_checkout_charges_the_card(
    client: Annotated[AsyncClient, Depends(api_client)],
) -> None:
    with mock.patch("app.billing.gateway.charge", return_value={"status": "succeeded"}):
        response = await client.post("/checkout", json={"amount": 4200})

    assert response.status_code == 200
```

`@voci.isolated` works too, for a patch that needs its own subprocess rather than just a turn
running alone.

## Cost when `unittest.mock` isn't used

voci only installs this guard once `unittest.mock` is already in `sys.modules` — that is, once
some test file has imported it. A suite that never imports `unittest.mock` pays nothing for this
check.

`examples/02-async-library/tests/test_patching.py` walks the full mocking ladder in one file, from
a `MagicMock` used as a plain value up through this page's decorator and context-manager cases —
see that example's README for the rest of the suite it sits in. [Marks](../reference/marks.md)
covers the full reference for `@voci.solo` and `@voci.isolated`.
