import json
import sys

sys.path.insert(0, "/tmp/fa")
import adapter
import staticparse


def load_state(path):
    d = json.load(open(path))
    table = []
    for e in d["table"]:
        e = dict(e)
        if e["methods"] is not None:
            e["methods"] = tuple(e["methods"])
        table.append(adapter.RouteEntry(**e))
    regs = []
    for r in d["regs"]:
        r = dict(r)
        if r["methods"] is not None:
            r["methods"] = tuple(r["methods"])
        regs.append(staticparse.RouteRegistration(**r))
    return table, regs


def load_collectors(path):
    d = json.load(open(path))
    out = {}
    for test_id, v in d.items():
        c = adapter.Collector(test_id)
        c.requests = {tuple(x) for x in v["requests"]}
        c.used_route_table = v["used_route_table"]
        c.matched_routes = {tuple(x) for x in v["matched_routes"]}
        out[test_id] = c
    return out


def load_outcomes(path):
    d = json.load(open(path))
    return {test_id: v.get("outcome") for test_id, v in d.items()}
