"""The mocking ladder, in one file, cheapest rung first.

velox ships **no patching API of its own**. `unittest.mock` is already concurrency-correct where it
matters — `MagicMock`, `AsyncMock`, `create_autospec`, the call assertions are all per-instance
state — and velox never wraps any of it. The one thing that is not safe is the *installer*:
`mock.patch` does a real `setattr` on a module or class, and every concurrently-running test sees
it.

So velox's job here is not to reimplement patching. It is to *notice* patching and schedule around
it.
"""

from __future__ import annotations

import os
from unittest import mock

import velox
from velox import Depends

from relay.cache import FakeClock, TTLCache
from relay.client import Relay
from relay.settings import Settings
from relay.transport import FakeTransport, Response, Transport
from tests.fixtures import flaky_relay, flaky_transport, relay, transport

# ========================================================================================
# Tier (a) — dependency injection. Fully concurrent. What the docs teach first.
# ========================================================================================


async def test_transport_substituted_by_di(
    r: Relay = Depends(flaky_relay),
    t: FakeTransport = Depends(flaky_transport),
) -> None:
    """No patching at all. Runs alongside every other test in the suite."""
    await r.deliver("https://hooks.test/v1", b"x")
    assert len(t.sent) == 3


async def test_transport_substituted_by_a_mock_object(r: Relay = Depends(relay)) -> None:
    """A `MagicMock` is a *value*, not a global write. It is completely fine here.

    `create_autospec` against the `Transport` protocol gives call assertions and a signature check,
    with no process-global state and therefore no scheduling cost. If what you want from
    `unittest.mock` is assertions rather than installation, you pay nothing.
    """
    fake: Transport = mock.create_autospec(Transport, instance=True)
    fake.post.return_value = Response(200, b"ok")

    relayed = Relay(fake, retries=1, base_delay=0.0)
    await relayed.deliver("https://hooks.test/v1", b"payload")

    fake.post.assert_awaited_once_with("https://hooks.test/v1", b"payload")


async def test_clock_substituted_by_di() -> None:
    """The jitter's evil twin. `TTLCache` takes its clock as an argument, so time is a parameter.

    This is the same capability `freezegun` sells, for free, concurrently, in one constructor
    argument.
    """
    clock = FakeClock()
    cache = TTLCache(ttl=10.0, clock=clock)
    cache.put("k", b"v")

    clock.advance(11.0)

    assert cache.get("k") is None


async def test_settings_substituted_by_di() -> None:
    """`Settings.from_env` takes the mapping. No `setenv`, no `patch.dict`, no solo.

    velox provides no `monkeypatch.setenv` equivalent: it would add an API without adding a
    capability, since the underlying `os.environ` write is global either way.
    """
    settings = Settings.from_env({"RELAY_RETRIES": "7", "RELAY_CACHE_TTL": "1.5"})

    assert settings.retries == 7
    assert settings.cache_ttl == velox.approx(1.5)


# ========================================================================================
# Tier (b) — stock `unittest.mock`. Installs by mutating a module or class, so it needs
# solo scheduling to be safe alongside other concurrent tests.
# ========================================================================================


@mock.patch("relay.client.random.uniform", return_value=1.0)
async def test_jitter_is_deterministic(
    uniform: mock.MagicMock,
    r: Relay = Depends(flaky_relay),
    t: FakeTransport = Depends(flaky_transport),
) -> None:
    """The mock parameter comes first, exactly as under pytest — `mock.patch` injects positionally.

    Injected parameters come after it and are resolved as usual: velox reads this test's
    dependencies off the function underneath the decorator, and `unittest.mock` fills the leading
    parameters it owns. `relay.client.random.uniform` is a module global, though, so this test is
    scheduled to run alone and the suite drains around it — the summary line at the end of the run
    says how long that took.

    The alternative is a `jitter: Callable[[], float]` argument on `Relay`. That makes this tier
    (a), and free of the scheduling cost.
    """
    await r.deliver("https://hooks.test/v1", b"x")

    assert len(t.sent) == 3
    assert uniform.call_count == 2


@mock.patch.dict(os.environ, {"RELAY_RETRIES": "9"})
async def test_settings_from_the_real_environment() -> None:
    """`test_settings_substituted_by_di` above is the same assertion for none of the cost.

    `mock.patch.dict` writes to the real `os.environ`, which is as global as any other patch, so
    this one runs alone too.
    """
    assert Settings.from_env().retries == 9


# `with mock.patch(...)` inside a body has no patcher object to find until the line runs, so
# `@velox.solo` is written by hand here. Without it the patch is refused as it installs and the
# test fails, naming the target — a patch nothing scheduled around is never quietly allowed to
# reach a module every other running test reads.
@velox.solo
async def test_context_manager_patching_must_be_marked(
    r: Relay = Depends(relay),
    t: FakeTransport = Depends(transport),
) -> None:
    t.responses = [Response(503), Response(200)]

    with mock.patch("relay.client.random.uniform", return_value=1.0):
        response = await r.deliver("https://hooks.test/v1", b"x")

    assert response.status == 200


# ========================================================================================
# Tier (d) — subprocess isolation, for state with no per-task view at all.
# ========================================================================================


# `chdir` has no per-task equivalent in CPython — one cwd per process — so this test is marked
# `@velox.isolated` rather than run for real against the process every other test shares.
@velox.isolated
async def test_relative_path_resolution() -> None:
    os.chdir("/tmp")
    assert os.getcwd() == "/tmp"
