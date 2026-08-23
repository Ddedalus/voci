# Worked examples

Both from `corpus/hazards_showcase`, whose audit reports `serialized_percent: 95.7` over 47 tests.

## 1 — The finding that carries the suite

```json
{"code": "VX401", "message": "`monkeypatch.setenv` writes an environment variable every test in flight can see.",
 "file": "conftest.py", "line": 43, "function": "patched_env", "tests": [ ... 45 node ids ... ]}
{"code": "VX402", "message": "`os.environ.pop(...)` writes the process environment.",
 "file": "conftest.py", "line": 45, "function": "patched_env", "tests": [ ... 45 node ids ... ]}
```

Two findings, one fixture, 45 of 47 tests each. Top of the queue by `len(.tests)`, and the gate
rule sends it to the seam: 45 `@velox.solo` marks would be a serial suite with extra syntax.

In the converted tree the file is `fixtures.py`, and this fixture is still pytest-spelled: velox
has no `monkeypatch`, so nothing about it converted.

```python
# fixtures.py
@pytest.fixture(autouse=True)
def patched_env(monkeypatch):
    monkeypatch.setenv("SUITE_MODE", "test")
    yield
    os.environ.pop("LEFTOVER", None)
```

After — the value is suite-wide and constant, so it leaves the fixture entirely, and the fixture
with it:

```toml
# pyproject.toml
[tool.velox]
env = { SUITE_MODE = "test" }
```

`LEFTOVER` was teardown for a variable some *test* set; that test is its own `VX402` finding lower
in the queue. Delete a shared fixture's teardown only after the writes it was cleaning up are
gone.

Where the value is not suite-wide — different tests need different values — the seam is an
injected settings object rather than the environment. Match the spelling `convert` emits
(`import velox`, `from velox import Depends`, no annotation on injected parameters):

```python
# fixtures.py
@velox.fixture()
def settings():
    return Settings(mode="test")


# test_it.py
def test_it(settings=Depends(settings)):
    assert settings.mode == "test"
```

Result: 45 tests leave the serialized set for one config line and no marks.

## 2 — Findings that are genuinely one test each

```json
{"code": "VX401", "message": "`monkeypatch.setattr` rebinds an attribute every test in flight shares.",
 "file": "test_concurrency.py", "line": 36, "function": "test_monkeypatched_attribute", "tests": ["test_concurrency.py::test_monkeypatched_attribute"]}
{"code": "VX401", "message": "`monkeypatch.chdir` changes the working directory, so this test needs isolation.",
 "file": "test_concurrency.py", "line": 37, "function": "test_monkeypatched_attribute", "tests": ["test_concurrency.py::test_monkeypatched_attribute"]}
```

One test, `.tests` of one, body site — marks apply. Two findings on one test, and the worse mark
wins: `chdir` is `@velox.isolated`, which subsumes the `setattr` finding's `@velox.solo`.

```python
import velox


@velox.isolated
def test_monkeypatched_attribute():
    with mock.patch.object(socket, "gethostname", lambda: "fake"):
        os.chdir(os.getcwd())
        assert socket.gethostname() == "fake"
```

`monkeypatch` has no velox counterpart, so the rebinding is hand-ported; the context-manager patch
is legal here only because the test is `@velox.isolated` — the guard admits a `with mock.patch(...)`
from a test marked `solo` or `isolated` and raises `GlobalPatchError` from any other.

Had `chdir` not been there, the `setattr` alone is better answered by a seam — pass the hostname
resolver in as a fixture — since one `@velox.solo` still drains the run for that test's duration.

## 3 — A contended resource, not a global write

```json
{"code": "VX413", "message": "`port=5432` is a fixed port number.", "file": "test_concurrency.py", "line": 22}
```

Not in the serialized set: nothing is written process-wide, but two tests cannot bind one port at
once. `exclusive=` is the cheapest answer — it serializes those tests against each other and
nothing else against anything.

```python
@velox.fixture(exclusive="pg-5432")
def server():
    return _make_server(5432)
```

Prefer allocating a free port per test where the resource allows it; `exclusive=` is for the
resource that is genuinely singular.
