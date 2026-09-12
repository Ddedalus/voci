import json
import sys

sys.path.insert(0, "/tmp/fa")
import adapter  # noqa: E402

adapter.install()

import pytest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app.main import app as _app  # noqa: E402

adapter.wrap_introspection(_app)

_collectors: dict[str, adapter.Collector] = {}


@pytest.fixture(scope="session")
def app():
    return _app


@pytest.fixture(scope="session")
def client(app):
    with TestClient(app) as c:
        yield c
_DUMP_PATH = "/tmp/fa/scripts/_collectors.json"


@pytest.fixture(autouse=True)
def _voci_collector(request):
    c = adapter.Collector(request.node.nodeid)
    _collectors[request.node.nodeid] = c
    with adapter.use_collector(c):
        yield c


_outcomes: dict[str, str] = {}


def pytest_runtest_logreport(report):
    if report.when == "call":
        _outcomes[report.nodeid] = report.outcome
    elif report.when in ("setup", "teardown") and report.outcome != "passed":
        _outcomes.setdefault(report.nodeid, report.outcome)


def pytest_sessionfinish(session, exitstatus):
    import os

    dump = {
        test_id: {
            "requests": sorted(c.requests),
            "used_route_table": c.used_route_table,
            "matched_routes": sorted([list(m) for m in c.matched_routes]),
            "outcome": _outcomes.get(test_id),
        }
        for test_id, c in _collectors.items()
    }
    out_path = os.environ.get("VOCI_DUMP_PATH", _DUMP_PATH)
    with open(out_path, "w") as f:
        json.dump(dump, f, indent=2)
