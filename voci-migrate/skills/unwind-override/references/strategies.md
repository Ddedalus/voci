# Strategies, before and after

Running example, `corpus/fixtures_showcase` — `settings` overridden in `integration/`,
`fan_out=2` (`settings` + `engine`), refused at `--budget 1`.

```python
# conftest.py
@pytest.fixture(scope="session")
def settings():
    return {"dsn": "sqlite://"}


@pytest.fixture
def engine(settings):
    return f"engine:{settings['dsn']}"


# integration/conftest.py
@pytest.fixture
def settings(settings):  # the [-2] super pattern
    return {**settings, "dsn": "sqlite://integration"}


# integration/test_integration.py
def test_engine(engine):
    assert engine == "engine:sqlite://integration"
```

---

## 0 — De-autouse (`VC027` only)

`VC027` fires when any of winner, `[-2]` or `D` is `autouse=True`. Drop `autouse` from every
autouse definition in the chain and name the fixture where it applied. Module-level
`pytestmark` converts as `VC009` (mechanical); a mark on individual tests converts as `VC010`
(marker), so prefer the module.

```python
# before — conftest.py and integration/conftest.py both
@pytest.fixture(autouse=True)
def clock():
    yield FakeClock()


# after — conftest.py and integration/conftest.py both
@pytest.fixture
def clock():
    yield FakeClock()


# after — every test module the fixture reached, from autouse_by_node
pytestmark = pytest.mark.usefixtures("clock")
```

Re-audit: the finding becomes `VC005` or `VC006`. Continue with strategies 1–5.

## 1 — Delete the override

Holds when the base can produce a value every test accepts, in and out of the subtree. Typical
shapes: the override only widens a timeout, adds a key nothing outside reads, or restates a
default that has since become the base's.

```python
# before — integration/conftest.py
@pytest.fixture
def http_timeout(http_timeout):
    return http_timeout * 3


# after — conftest.py, override file deleted
@pytest.fixture
def http_timeout():
    return 30  # was 10; the subtree's 30 is fine everywhere
```

Verify against the whole suite, not just `.tests` — this changes what tests *outside* the node
get, which is the one strategy that does.

## 2 — Move `D` down

Per member of `detail.duplicated` reported `MOVABLE` by `chain-analysis.md`'s query: move its `def`
into the overriding directory's `conftest.py`. No test's resolution changes.

```python
# after — conftest.py
@pytest.fixture(scope="session")
def settings():
    return {"dsn": "sqlite://"}


# `engine` gone from here


# after — integration/conftest.py
@pytest.fixture
def settings(settings):
    return {**settings, "dsn": "sqlite://integration"}


@pytest.fixture
def engine(settings):  # unchanged body, new home
    return f"engine:{settings['dsn']}"
```

`fan_out` 2 → 1. `--budget 1` now converts it. Test signatures are untouched.

## 3 — Rename and request

The subtree wants a genuinely different object. Give it its own name, rename each consumer under
the node, and point the subtree's tests at the new names. Nothing overrides anything afterwards,
so the finding disappears rather than shrinking.

```python
# after — conftest.py unchanged, integration/conftest.py
@pytest.fixture
def integration_settings(settings):  # requests the base by its own name
    return {**settings, "dsn": "sqlite://integration"}


@pytest.fixture
def integration_engine(integration_settings):
    return f"engine:{integration_settings['dsn']}"


# after — integration/test_integration.py
def test_engine(integration_engine):
    assert integration_engine == "engine:sqlite://integration"
```

Cost: every test signature under the node that named a renamed fixture. Worth it at `|D| ≤ 2`, or
after strategy 4.

## 4 — Factory seam, then 3

At large `|D|`, strategy 3 means retyping bodies. Lift each body into a plain helper module first
— not a `conftest.py`, so nothing about fixture visibility changes — and leave one-line fixtures
behind. Then strategy 3's specialized chain is one line per fixture.

```python
# new — tests/factories.py
def make_engine(settings):
    return f"engine:{settings['dsn']}"


def make_session(engine):
    return Session(engine)


# after — conftest.py
from tests import factories


@pytest.fixture
def engine(settings):
    return factories.make_engine(settings)


@pytest.fixture
def session(engine):
    return factories.make_session(engine)


# after — integration/conftest.py, the whole specialized chain
@pytest.fixture
def integration_settings(settings):
    return {**settings, "dsn": "sqlite://integration"}


@pytest.fixture
def integration_engine(integration_settings):
    return factories.make_engine(integration_settings)


@pytest.fixture
def integration_session(integration_engine):
    return factories.make_session(integration_engine)
```

Run pytest after the lift alone, before renaming anything: the lift must be a no-op, and proving
that separately is what makes the rename reviewable.

## 5 — Narrow the node

The override rules a directory but only one module's tests need it. A fixture defined in a test
module is visible only in that module, so moving the `def` there shrinks `.tests` — which shrinks
the refusal's blast radius, and re-runs strategy 2's `MOVABLE` check against a smaller test set,
usually turning a `shared:` member into a movable one.

```python
# after — integration/conftest.py loses the override; integration/test_integration.py gains it
# and, once `engine` is reached only from here, gains that too (strategy 2 at module scope)
@pytest.fixture
def settings(settings):
    return {**settings, "dsn": "sqlite://integration"}


@pytest.fixture
def engine(settings):
    return f"engine:{settings['dsn']}"


def test_engine(engine): ...
```

The tests you dropped now resolve the *base* `settings`. If any of them was relying on the
override without saying so, the full-suite run at step 3 is what catches it.
