"""Task 1: verify per-test attribution under every concurrency shape voci cares about."""
import asyncio
import contextvars
import sys
import threading

sys.path.insert(0, "/tmp/fa")
import adapter  # noqa: E402

adapter.install()

import httpx  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route as SRoute  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app.main import app as fastapi_app  # noqa: E402

failures = []


def check(name, collector, expected):
    ok = collector.requests == expected
    print(f"[{'OK ' if ok else 'FAIL'}] {name}: {sorted(collector.requests)}" + ("" if ok else f"  expected {sorted(expected)}"))
    if not ok:
        failures.append(name)


# --- bare Starlette app, for the "check both FastAPI and bare Starlette" ask ---
async def s_a(request):
    return JSONResponse({"a": True})


async def s_b(request):
    return JSONResponse({"b": True})


starlette_app = Starlette(routes=[SRoute("/a", s_a), SRoute("/b", s_b)])

# =====================================================================
# 1. module-level TestClient(app), one collector set per "test"
# =====================================================================
module_client = TestClient(fastapi_app)
c1 = adapter.Collector("module-level-test-1")
with adapter.use_collector(c1):
    module_client.get("/users/1")
c2 = adapter.Collector("module-level-test-2")
with adapter.use_collector(c2):
    module_client.get("/users/2")
    module_client.get("/legacy/health")
check("module-level TestClient, test 1", c1, {("GET", "/users/1")})
check("module-level TestClient, test 2", c2, {("GET", "/users/2"), ("GET", "/legacy/health")})

# =====================================================================
# 2. persistent `with TestClient(app) as c` from a "session fixture",
#    used across several "tests" that each set their own collector
# =====================================================================
with TestClient(fastapi_app) as persistent_client:
    cf1 = adapter.Collector("fixture-test-1")
    with adapter.use_collector(cf1):
        persistent_client.get("/users/10")
    cf2 = adapter.Collector("fixture-test-2")
    with adapter.use_collector(cf2):
        persistent_client.get("/users/20")
        persistent_client.get("/api/admin/reports/summary")
    check("persistent portal, test 1", cf1, {("GET", "/users/10")})
    check("persistent portal, test 2", cf2, {("GET", "/users/20"), ("GET", "/api/admin/reports/summary")})

# =====================================================================
# 3. httpx.AsyncClient(transport=ASGITransport(app)) in async tests
#    interleaving on one loop (asyncio.gather of two "tests")
# =====================================================================


async def async_test(name, path, barrier):
    collector = adapter.Collector(name)
    with adapter.use_collector(collector):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(fastapi_app), base_url="http://t") as ac:
            await barrier.wait()  # force interleaving: both tasks mid-request at once
            await ac.get(path)
    return collector


async def run_async_interleave():
    barrier = asyncio.Barrier(2)
    coll_a, coll_b = await asyncio.gather(
        async_test("async-test-A", "/from-variable", barrier),
        async_test("async-test-B", "/multi", barrier),
    )
    check("asyncio.gather interleaved, test A", coll_a, {("GET", "/from-variable")})
    check("asyncio.gather interleaved, test B", coll_b, {("GET", "/multi")})


asyncio.run(run_async_interleave())

# =====================================================================
# 4. two sync "tests" driving TestClient from two OS threads at once
#    (simulating voci's concurrent test workers) -- contextvars do NOT
#    propagate into a new thread by default, so each thread's target
#    sets its own collector right at entry, the way a real per-test
#    ContextVar assignment would on whichever thread runs the test.
# =====================================================================
thread_results = {}


def thread_target(name, path):
    collector = adapter.Collector(name)
    with adapter.use_collector(collector):
        module_client.get(path)
    thread_results[name] = collector


t1 = threading.Thread(target=thread_target, args=("thread-test-1", "/legacy/anything"))
t2 = threading.Thread(target=thread_target, args=("thread-test-2", "/manual"))
t1.start()
t2.start()
t1.join()
t2.join()
check("thread 1", thread_results["thread-test-1"], {("GET", "/legacy/anything")})
check("thread 2", thread_results["thread-test-2"], {("GET", "/manual")})

# Now the failure mode the plan calls out: a collector set in the *main*
# thread is invisible to a bare `threading.Thread(target=...)` unless the
# target itself sets one, because contextvars don't cross thread creation.
leak_collector = adapter.Collector("main-thread-collector")
leaked = {}


def naive_thread_target():
    # does NOT set its own collector -- relies on inheriting the parent's
    leaked["seen"] = adapter.current_collector.get()
    module_client.get("/users/999")  # would silently attribute to nothing


with adapter.use_collector(leak_collector):
    t3 = threading.Thread(target=naive_thread_target)
    t3.start()
    t3.join()
print(
    f"[{'OK ' if leaked['seen'] is None else 'FAIL'}] contextvar does NOT propagate to a bare new thread "
    f"(seen={leaked['seen']!r}); a plain Thread.start() would silently drop this request unless voci "
    "patches Thread.start to carry the creator's context, as the plan already calls for"
)
check("main thread collector unaffected by orphan thread's request", leak_collector, set())

# =====================================================================
# 5. websockets via client.websocket_connect
# =====================================================================
cws = adapter.Collector("ws-test")
with adapter.use_collector(cws):
    with module_client.websocket_connect("/ws/echo") as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "ping"
check("websocket_connect", cws, {("WEBSOCKET", "/ws/echo")})

# =====================================================================
# 6. bare Starlette (not FastAPI) -- same Router.__call__ patch applies
# =====================================================================
starlette_client = TestClient(starlette_app)
cs = adapter.Collector("bare-starlette-test")
with adapter.use_collector(cs):
    starlette_client.get("/a")
    starlette_client.get("/b")
check("bare Starlette app", cs, {("GET", "/a"), ("GET", "/b")})

print()
if failures:
    print(f"FAILURES: {failures}")
    sys.exit(1)
print("all recorder scenarios passed")
