"""Task 3/4: the selection-time predictor. Combines the runtime route table
diff (old vs new), the static kwarg diff, and each test's recorded requests /
matched_routes / used_route_table flag into a predicted affected set.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from starlette.routing import compile_path

import staticparse


def path_matches(entry_path: str, method_or_path_only: str) -> bool:
    """True if `entry_path`'s pattern matches this literal request path at
    all (FULL or PARTIAL -- method is intentionally ignored: a path overlap
    alone can turn a 404 into a 405, or change which handler serves a 200)."""
    regex, _, _ = compile_path(entry_path)
    return regex.match(method_or_path_only) is not None


def _key(entry: Any) -> tuple[str | None, str | None]:
    return (entry.endpoint_file, entry.endpoint_qualname)


@dataclasses.dataclass
class PredictionDetail:
    affected: set[str]
    reasons: dict[str, list[str]] = dataclasses.field(default_factory=dict)

    def add(self, test_ids: set[str], reason: str) -> None:
        for t in test_ids:
            self.affected.add(t)
            self.reasons.setdefault(t, []).append(reason)


def tests_matching_path(collectors: dict[str, Any], entry_path: str) -> set[str]:
    out = set()
    for test_id, c in collectors.items():
        for _method, path in c.requests:
            if path_matches(entry_path, path):
                out.add(test_id)
                break
    return out


def tests_with_matched_route(collectors: dict[str, Any], key: tuple[str | None, str | None]) -> set[str]:
    file, qualname = key
    out = set()
    for test_id, c in collectors.items():
        for mfile, mqual, _lineno in c.matched_routes:
            if mfile == file and mqual == qualname:
                out.add(test_id)
                break
    return out


def tests_using_route_table(collectors: dict[str, Any]) -> set[str]:
    return {t for t, c in collectors.items() if c.used_route_table}


def predict_affected(
    old_table: list,
    new_table: list,
    old_regs: list,
    new_regs: list,
    collectors: dict[str, Any],
) -> PredictionDetail:
    """`old_regs`/`new_regs` are flat lists of staticparse.RouteRegistration
    across every first-party file, from a parse of the source *before* and
    *after* the edit."""
    result = PredictionDetail(affected=set())

    routeish_old = {_key(e): e for e in old_table if e.kind in ("route", "websocket")}
    routeish_new = {_key(e): e for e in new_table if e.kind in ("route", "websocket")}

    _REG_KINDS = ("decorator", "add_api_route", "add_route")
    old_reg_by_key = {(r.file, r.endpoint_qualname): r for r in old_regs if r.kind in _REG_KINDS}
    new_reg_by_key = {(r.file, r.endpoint_qualname): r for r in new_regs if r.kind in _REG_KINDS}

    added = set(routeish_new) - set(routeish_old)
    removed = set(routeish_old) - set(routeish_new)
    common = set(routeish_old) & set(routeish_new)

    table_changed = bool(added or removed)

    for key in added:
        entry = routeish_new[key]
        result.add(tests_matching_path(collectors, entry.path), f"new route {entry.path!r} matches recorded request")

    for key in removed:
        entry = routeish_old[key]
        result.add(tests_matching_path(collectors, entry.path), f"removed route {entry.path!r} matched a recorded request")

    for key in common:
        old_e, new_e = routeish_old[key], routeish_new[key]
        if old_e.path != new_e.path or old_e.methods != new_e.methods:
            table_changed = True
            result.add(tests_matching_path(collectors, old_e.path), f"route {key} path/methods changed (old pattern {old_e.path!r})")
            result.add(tests_matching_path(collectors, new_e.path), f"route {key} path/methods changed (new pattern {new_e.path!r})")
        if old_e.name != new_e.name:
            table_changed = True
            result.add(tests_using_route_table(collectors), f"route {key} name changed ({old_e.name!r} -> {new_e.name!r}), affects reverse lookups")

        old_reg, new_reg = old_reg_by_key.get(key), new_reg_by_key.get(key)
        if old_reg is not None and new_reg is not None:
            change = staticparse.classify_kwarg_change(old_reg, new_reg)
            if change == "behavioral":
                table_changed = True
                result.add(tests_with_matched_route(collectors, key), f"route {key} behavioral kwarg changed")
            elif change == "openapi-only":
                result.add(tests_using_route_table(collectors), f"route {key} OpenAPI-only kwarg changed")

    if table_changed:
        result.add(tests_using_route_table(collectors), "route table changed at all; openapi()/url_path_for tests depend on the whole table")

    return result


def registration_order(regs: list) -> dict[str, list[str]]:
    """file -> ordered list of endpoint_qualname for decorator registrations,
    in source order, to detect a pure reordering edit."""
    out: dict[str, list[str]] = {}
    for r in sorted((r for r in regs if r.kind == "decorator"), key=lambda r: (r.file, r.lineno)):
        out.setdefault(r.file, []).append(r.endpoint_qualname)
    return out


def detect_pure_reorder(old_table, new_table, old_regs, new_regs) -> set[str]:
    """Files where the route table is byte-for-byte identical (no add/remove/
    modify at all) but two routes' relative registration order swapped --
    invisible to `predict_affected` above, since nothing about any individual
    route changed. Returns the set of files needing a coarse fallback."""
    routeish_old = {_key(e): e for e in old_table if e.kind in ("route", "websocket")}
    routeish_new = {_key(e): e for e in new_table if e.kind in ("route", "websocket")}
    if set(routeish_old) != set(routeish_new):
        return set()
    for key in routeish_old:
        if routeish_old[key].path != routeish_new[key].path or routeish_old[key].methods != routeish_new[key].methods:
            return set()

    old_order = registration_order(old_regs)
    new_order = registration_order(new_regs)
    changed_files = set()
    for file, order in old_order.items():
        if new_order.get(file) != order and set(new_order.get(file, [])) == set(order):
            changed_files.add(file)
    return changed_files
