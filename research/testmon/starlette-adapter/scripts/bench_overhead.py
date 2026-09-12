import statistics
import sys
import time

sys.path.insert(0, "/tmp/fa")
import adapter
from starlette.testclient import TestClient

from app.main import app

N = 2000


def bench(label, client):
    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        for _ in range(N):
            client.get("/users/5")
        times.append((time.perf_counter() - t0) / N * 1e6)  # us/request
    print(f"{label:24} median={statistics.median(times):.1f}us  min={min(times):.1f}us  (n={N} req/round)")


client = TestClient(app)
bench("unpatched", client)

adapter.install()
c = adapter.Collector("bench")
with adapter.use_collector(c):
    bench("patched, recording", client)

with adapter.use_collector(None):
    bench("patched, no collector", client)
