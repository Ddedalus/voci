# Guide

An ordered walkthrough of velox, one concept per page. Start here and read forward; each page
assumes the ones before it.

## Install

velox needs Python 3.13 or newer. The core package has no dependencies.

```bash
uv pip install velox-test          # or: pip install velox-test
```

The distribution is `velox-test`; the package you import is `velox`.

## Your first test

A test is an `async def` (or plain `def`) function whose name starts with `test_`, in a file named
`test_*.py` or `*_test.py`:

```python
# tests/test_math.py
async def test_addition() -> None:
    assert 1 + 1 == 2
```

Run the suite from the project root:

```bash
velox
```

```console
config: none
PASS  tests/test_math.py                          1 test   Σ 0.00s

1 test · 1 passed · 0.02s wall (0.0x concurrency)
```

The first line names the `[tool.velox]` table the run picked up, or `none`. The `Σ` column is the
sum of every test's own duration in that file; the last line's wall time is how long the run
actually took, and the multiplier is the ratio between the two. One instant test has nothing to
overlap, so the multiplier only becomes interesting once the suite has tests that wait on
something.

## The shape of a suite

A velox suite is ordinary Python modules. There is no `conftest.py` and no name-based lookup —
anything shared is a function you import.

```
tests/
  fixtures.py        shared fixtures, imported by the test files that want them
  test_users.py
  test_orders.py
```

A fixture is a function decorated with `@velox.fixture()`. A test — or another fixture — asks for
one by naming `Depends(that_function)` in a parameter's `Annotated[...]` metadata:

```python
# tests/fixtures.py
import velox


@velox.fixture()
def settings() -> Settings:
    return Settings(endpoint="https://hooks.test/v1", retries=3)


# tests/test_delivery.py
from typing import Annotated

from velox import Depends

from tests.fixtures import settings


async def test_endpoint_is_versioned(config: Annotated[Settings, Depends(settings)]) -> None:
    assert config.endpoint.endswith("/v1")
```

Because the fixture arrives as an imported name rather than a string, "go to definition" lands on
it, renames are safe, and a misspelling is an `ImportError` at collection time.

`Depends(that_function)` also works in a parameter's default — `config: Settings =
Depends(settings)` — which is shorter to write. [Fixtures](../reference/fixtures.md#where-the-injection-is-declared)
covers what that form costs you, and why `Annotated` is the one this guide teaches.
