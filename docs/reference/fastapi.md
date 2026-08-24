# FastAPI

`velox.fastapi` gives each test its own dependency overrides and its own `app.state` against the
one module-level `app = FastAPI()` your application already defines. Both of those are per-app
data in FastAPI, shared by every test that touches the app; velox swaps each attribute, once per
app, for a proxy that layers this test's values over the app's own, so tests running concurrently
read and write their own layer through the same app object.

The module needs `fastapi` and `httpx`, which the core package does not require, and
`velox/__init__.py` does not import it. Reach it by name:

```python
import velox.fastapi
```

::: velox.fastapi.client

::: velox.fastapi.lifespan

::: velox.fastapi.uninstall

The type of both halves of an `overrides` mapping, and the same callable FastAPI itself keys on:

::: velox.fastapi.Override
