import sys

sys.path.insert(0, "/tmp/fa")
import adapter  # noqa: E402

adapter.install()

from starlette.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


def run(name, method, path, **kw):
    c = adapter.Collector(name)
    with adapter.use_collector(c):
        resp = client.request(method, path, **kw)
    print(f"{name:22} status={resp.status_code:3} requests={sorted(c.requests)} matched_routes={[q for _, q, _ in c.matched_routes]}")
    return resp


run("plain-200", "GET", "/users/5")
run("plain-404", "GET", "/does-not-exist")
run("method-405", "DELETE", "/validated/search")  # search only supports GET -> 405, PARTIAL match
run("body-422", "POST", "/validated/orders", json={"quantity": -3})  # violates gt=0 -> 422, handler body never runs
run("dep-raises-401", "GET", "/validated/protected", headers={"x-token": "wrong"})  # Depends raises before handler
run("dep-ok-200", "GET", "/validated/protected", headers={"x-token": "secret"})
run("websocket-echo", "GET", "/ws/echo")  # not applicable via .get; separate below

# websocket via TestClient
c = adapter.Collector("ws-connect")
with adapter.use_collector(c):
    with client.websocket_connect("/ws/echo") as ws:
        ws.send_text("hi")
        assert ws.receive_text() == "hi"
print(f"{'ws-connect':22} requests={sorted(c.requests)} matched_routes={[q for _, q, _ in c.matched_routes]}")
