# FastAPI

Velox provides utilities to work with FastAPI applications more easily.
FastAPI exposes two mutable objects on the app, which could break under concurrent test execution: state and dependency overrides.

Velox allows you to swap these out for asyncio-aware proxies, which behave exactly the same, but isolate test cases.

You must have `fastapi` and `httpx` installed to import these objects.

```python
import velox.fastapi
```

::: velox.fastapi.client

::: velox.fastapi.lifespan

::: velox.fastapi.uninstall

The type of both halves of an `overrides` mapping, and the same callable FastAPI itself keys on:

::: velox.fastapi.Override
