import sys

sys.path.insert(0, "/tmp/fa")
import adapter
import staticparse

adapter.install()
from app.main import app

table = adapter.extract_routes(app)

files = [
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

all_regs = []
sibling_source_paths = {}
for f in files:
    src = open(f).read()
    regs = staticparse.parse_registrations(src, f)
    all_regs.extend(regs)
    for r in regs:
        if r.kind == "decorator" and r.literal_path is not None:
            sibling_source_paths[(r.file, r.endpoint_qualname)] = (r.receiver, r.literal_path)

print("=== all registrations found ===")
for r in all_regs:
    print(r.kind, r.receiver, r.receiver_is_simple, r.verb, r.literal_path, r.methods, r.endpoint_qualname, r.file.split("/")[-1], r.lineno)

print()
print("=== derive full path for a NEW hypothetical route in items.py ===")
new_reg = staticparse.RouteRegistration(
    kind="decorator", receiver="router", receiver_is_simple=True, verb="get",
    literal_path="/{item_id:int}/reviews", methods=("GET",), name_kwarg=None,
    lineno=999, endpoint_qualname="get_item_reviews", file="/tmp/fa/app/routers/items.py",
)
full, reason = staticparse.full_path_for_new_route(table, new_reg, sibling_source_paths)
print("predicted full path:", full, "| reason:", reason)

print()
print("=== derive full path for a NEW route in admin.py's reports_router (nested + include_router prefix) ===")
new_reg2 = staticparse.RouteRegistration(
    kind="decorator", receiver="reports_router", receiver_is_simple=True, verb="get",
    literal_path="/new-report", methods=("GET",), name_kwarg=None,
    lineno=999, endpoint_qualname="new_report", file="/tmp/fa/app/routers/admin.py",
)
full2, reason2 = staticparse.full_path_for_new_route(table, new_reg2, sibling_source_paths)
print("predicted full path:", full2, "| reason:", reason2)

print()
print("=== derive full path for a route with a non-literal path (dynamic.py) ===")
dyn_regs = [r for r in all_regs if r.file.endswith("dynamic.py")]
for r in dyn_regs:
    full3, reason3 = staticparse.full_path_for_new_route(table, r, sibling_source_paths)
    print(r.endpoint_qualname, "->", full3, "|", reason3)
