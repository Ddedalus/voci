"""Run in a fresh subprocess: import app.main fresh, extract the route table
and the static registrations for every first-party app file, dump as JSON.
Usage: python extract_state.py <output.json>
"""
import dataclasses
import json
import sys

sys.path.insert(0, "/tmp/fa")
import adapter
import staticparse

adapter.install()

from app.main import app  # noqa: E402

APP_FILES = [
    "/tmp/fa/app/main.py",
    "/tmp/fa/app/sub.py",
    "/tmp/fa/app/routers/items.py",
    "/tmp/fa/app/routers/users.py",
    "/tmp/fa/app/routers/legacy.py",
    "/tmp/fa/app/routers/admin.py",
    "/tmp/fa/app/routers/multi.py",
    "/tmp/fa/app/routers/ws.py",
    "/tmp/fa/app/routers/dynamic.py",
    "/tmp/fa/app/routers/validated.py",
]

table = adapter.extract_routes(app)
# keep only first-party routes (drop FastAPI's own /docs, /openapi.json, etc.)
table = [e for e in table if e.endpoint_file and e.endpoint_file.startswith("/tmp/fa/app")]

regs = []
for f in APP_FILES:
    regs.extend(staticparse.parse_registrations(open(f).read(), f))

out = {
    "table": [dataclasses.asdict(e) for e in table],
    "regs": [dataclasses.asdict(r) for r in regs],
}
with open(sys.argv[1], "w") as fh:
    json.dump(out, fh, indent=2)
