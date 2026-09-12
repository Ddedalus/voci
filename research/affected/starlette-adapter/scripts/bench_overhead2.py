import asyncio
import statistics
import sys
import time

sys.path.insert(0, "/tmp/fa")
import adapter
from app.main import app

N = 20000


async def fake_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def fake_send(msg):
    pass


def make_scope():
    return {
        "type": "http", "method": "GET", "path": "/users/5", "raw_path": b"/users/5",
        "query_string": b"", "headers": [], "asgi": {"version": "3.0"}, "http_version": "1.1",
        "scheme": "http", "server": ("test", 80), "client": ("test", 123), "root_path": "",
    }


async def run(label, n):
    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        for _ in range(n):
            await app(make_scope(), fake_receive, fake_send)
        times.append((time.perf_counter() - t0) / n * 1e6)
    print(f"{label:24} median={statistics.median(times):.2f}us/call  min={min(times):.2f}us")


async def main():
    await run("unpatched router", N)
    adapter.install()
    c = adapter.Collector("bench")
    with adapter.use_collector(c):
        await run("patched, recording", N)
    with adapter.use_collector(None):
        await run("patched, no collector", N)


asyncio.run(main())
