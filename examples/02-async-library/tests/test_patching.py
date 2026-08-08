"""The mocking ladder, in one file, cheapest rung first.

velox ships **no patching API of its own**. `unittest.mock` is already concurrency-correct where it
matters — `MagicMock`, `AsyncMock`, `create_autospec`, the call assertions are all per-instance
state — and velox never wraps any of it. The one thing that is not safe is the *installer*:
`mock.patch` does a real `setattr` on a module or class, and every concurrently-running test sees
it.

So velox's job here is not to reimplement patching. It is to *notice* patching and schedule around
it. See spec/08.
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

    velox provides no `monkeypatch.setenv` equivalent, deliberately: it would add an API without
    adding a capability, since the underlying `os.environ` write is global either way.
    """
    settings = Settings.from_env({"RELAY_RETRIES": "7", "RELAY_CACHE_TTL": "1.5"})

    assert settings.retries == 7
    assert settings.cache_ttl == velox.approx(1.5)


# ========================================================================================
# Tier (b) — stock `unittest.mock`, detected automatically, scheduled SOLO.
# ========================================================================================


@mock.patch("relay.client.random.uniform", return_value=1.0)
async def test_jitter_is_deterministic(
    uniform: mock.MagicMock,
    r: Relay = Depends(flaky_relay),
) -> None:
    """This runs alone. The whole suite drains for it.

    velox detects this without being told: `unittest.mock` sets `func.patchings` on the wrapper it
    returns, so collection does one `hasattr` per test and marks the ones that patch as solo. It
    also reads `p.target` off each patcher, so the report can say *what* was patched and why the
    suite stopped.

    The mock parameter comes first, exactly as under pytest — `mock.patch` injects positionally and
    velox's `Depends` defaults sit after it, so the migration of a decorated test is zero-diff.

    The honest version of this test is a `jitter: Callable[[], float]` argument on `Relay`. Then it
    is tier (a) and costs nothing. That refactor is what `velox migrate` reports as a "DI seam
    opportunity"; it does not perform it, because it cannot know if the seam is wanted.
    """
    await r.deliver("https://hooks.test/v1", b"x")

    assert uniform.call_count == 2


@mock.patch.dict(os.environ, {"RELAY_RETRIES": "9"})
async def test_settings_from_the_real_environment() -> None:
    """Also solo, and also avoidable — `test_settings_substituted_by_di` above is the same
    assertion for none of the cost. This one exists to show that `patch.dict` is detected by the
    same mechanism: it is a `_patch` object like any other.
    """
    assert Settings.from_env().retries == 9


@velox.solo
async def test_context_manager_patching_must_be_marked(
    r: Relay = Depends(relay),
    t: FakeTransport = Depends(transport),
) -> None:
    """`with mock.patch(...)` inside a body is **not** statically visible.

    There is no `patchings` attribute to find — the patch object does not exist until the line
    runs. So this one has to be marked by hand, and `@velox.solo` is that mark.

    You are not expected to remember: `velox migrate` finds these sites and adds the decorator, and
    at run time velox wraps `unittest.mock._patch.__enter__`, so an unmarked one fails with a
    message naming the target rather than silently racing. (Requeueing it as solo instead of
    failing is roadmap — it needs abort-and-rerun.)
    """
    t.responses = [Response(503), Response(200)]

    with mock.patch("relay.client.random.uniform", return_value=1.0):
        response = await r.deliver("https://hooks.test/v1", b"x")

    assert response.status == 200


# ========================================================================================
# Tier (d) — subprocess. Roadmap; shown so the ladder is complete.
# ========================================================================================


@velox.isolated
async def test_relative_path_resolution() -> None:
    """`chdir` has no per-task equivalent in CPython — there is one cwd per process, full stop.

    `@velox.isolated` runs this test in a subprocess on a fresh loop and ships the result back as
    JSON. Unlike `@velox.solo` it does *not* take the suite-wide write lock: it shares no process
    state, so it runs concurrently with everything else and only costs a spawn.
    """
    os.chdir("/tmp")
    assert os.getcwd() == "/tmp"
