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
@velox.skip(
    "would run unmarked and fully concurrent without real solo scheduling (M1-PLAN.md) -- and "
    "not only against another test patching the same target: relay.client.random.uniform is a "
    "module-global function, so *any* other concurrently-dispatched test that reaches Relay."
    "deliver()'s retry path (test_delivery.py has several; so does this file) calls the same "
    "patched name and pollutes this one's call_count too. Verified directly: with "
    "test_transport_substituted_by_di (tier (a), no patching of its own) left running alongside "
    "this one, call_count came back 3, not 2. Safe to re-enable once @velox.solo is enforced."
)
async def test_jitter_is_deterministic(uniform: mock.MagicMock) -> None:
    """`unittest.mock` sets `func.patchings` on the wrapper `@mock.patch` returns, so collection
    *could* do one `hasattr` per test and mark the ones that patch as solo, reading `p.target` off
    each patcher so the report can say what and why. That detection isn't wired up yet — see the
    `@velox.skip` reason above for what runs instead today, and what running this for real, right
    now, actually does to an unrelated neighbour.

    `_fixtures.plan_for` reads `func.__code__`/`func.__defaults__` directly, never unwraps
    `__wrapped__`, so it sees this wrapper's own `(*args, **keywargs)` shape, not the real
    parameter list underneath -- which means a `Depends(...)` default on this function would
    silently never resolve (M1-PLAN.md). `flaky_relay`/`flaky_transport` are built by hand below
    instead, sidestepping that separate gap so the only thing keeping this skipped is the real one.

    The mock parameter comes first, exactly as under pytest — `mock.patch` injects positionally.

    The honest version of this test is a `jitter: Callable[[], float]` argument on `Relay`. Then it
    is tier (a) and costs nothing. That refactor is what `velox migrate` reports as a "DI seam
    opportunity"; it does not perform it, because it cannot know if the seam is wanted.
    """
    t = FakeTransport(responses=[Response(503), Response(503), Response(200, b"ok")])
    r = Relay(t, retries=3, base_delay=0.001)

    await r.deliver("https://hooks.test/v1", b"x")

    assert uniform.call_count == 2


@mock.patch.dict(os.environ, {"RELAY_RETRIES": "9"})
async def test_settings_from_the_real_environment() -> None:
    """In a fuller suite this would also need to run solo, and also be avoidable —
    `test_settings_substituted_by_di` above is the same assertion for none of the cost. This one
    exists to show that `patch.dict` is detected the same way `mock.patch` itself would be: it is
    a `_patch` object like any other. (Not actually racing anything here: nothing else in this
    suite reads the real `os.environ`, so this one is safe to run live even without the solo
    scheduling that would make it safe in general — see `test_jitter_is_deterministic` above for
    what that gap is and why.)
    """
    assert Settings.from_env().retries == 9


# `with mock.patch(...)` inside a body is not statically visible the way the decorator above is —
# there is no `patchings` attribute to find until the line actually runs, so `velox migrate` would
# add `@velox.solo` here by hand, and at run time velox would (eventually -- roadmap, spec/08) wrap
# `unittest.mock._patch.__enter__` so an unmarked one fails loudly naming the target instead of
# silently racing. `@velox.solo` is recorded here already, and it is honest as *documentation* of
# intent -- but it isn't enforced yet either (same M1-PLAN.md gap as the decorator form), and this
# test patches the exact same target as `test_jitter_is_deterministic` above. Verified directly
# (a standalone `asyncio.TaskGroup` running the two bodies concurrently, 2000/2000 trials): without
# real exclusion, the two clobber each other's mock every time -- not a rare timing coincidence,
# a certainty once they actually overlap. Skipped, not run live, until solo is real.
@velox.solo
@velox.skip(
    "would race test_jitter_is_deterministic's patch of the same target without real solo "
    "scheduling (M1-PLAN.md); safe to re-enable once @velox.solo is enforced"
)
async def test_context_manager_patching_must_be_marked(
    r: Relay = Depends(relay),
    t: FakeTransport = Depends(transport),
) -> None:
    t.responses = [Response(503), Response(200)]

    with mock.patch("relay.client.random.uniform", return_value=1.0):
        response = await r.deliver("https://hooks.test/v1", b"x")

    assert response.status == 200


# ========================================================================================
# Tier (d) — subprocess. Roadmap; shown so the ladder is complete.
# ========================================================================================


# `@velox.isolated` would run this in a subprocess on a fresh loop and ship the result back as
# JSON -- unlike `@velox.solo` it wouldn't take the suite-wide write lock, since a subprocess
# shares no state with anything else, only costing a spawn. That subprocess tier doesn't exist yet
# (spec/00 §7 lists it "Deferred", separately from the rest of the patching ladder), so the mark
# below does nothing today, and the body's `os.chdir("/tmp")` would run for real, in *this*
# process, for the rest of the suite -- `chdir` has no per-task equivalent in CPython, one cwd per
# process, full stop. Skipped rather than run live for exactly that reason.
@velox.isolated
@velox.skip("@velox.isolated has no subprocess tier yet (M1-PLAN.md); os.chdir would be real here")
async def test_relative_path_resolution() -> None:
    os.chdir("/tmp")
    assert os.getcwd() == "/tmp"
