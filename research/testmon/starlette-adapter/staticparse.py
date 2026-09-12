"""Task 3: static side. Parse Starlette/FastAPI route registration statements
out of source, and derive a new route's full path from the runtime prefix of
its receiver router (read off the *last known* route table, i.e. the table
extracted from the app before this edit).
"""

from __future__ import annotations

import ast
import dataclasses
from starlette.routing import compile_path

DECORATOR_VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "api_route", "route", "websocket"}

# Decorator kwargs that are pure OpenAPI/documentation metadata: they never
# change what a request actually gets back, only what /openapi.json (and
# url_path_for's `name`, handled separately) describes. Everything else
# (response_model, status_code, dependencies, ...) can change a response body,
# status code, or what runs before the handler -- "behavioral".
OPENAPI_ONLY_KWARGS = {"tags", "summary", "description", "deprecated", "operation_id", "include_in_schema", "responses", "openapi_extra"}


@dataclasses.dataclass
class RouteRegistration:
    kind: str  # "decorator" | "add_api_route" | "add_route" | "include_router"
    receiver: str | None  # simple Name receiver; None if not a simple Name (Attribute etc.)
    receiver_is_simple: bool
    verb: str | None  # get/post/.../include_router/add_api_route/add_route
    literal_path: str | None  # None if not a string literal (dynamic)
    methods: tuple[str, ...] | None
    name_kwarg: str | None
    lineno: int  # first decorator's line for a decorated def; the call's line otherwise
    endpoint_qualname: str | None  # best-effort: the decorated def's qualname, or a resolved add_api_route/add_route target
    file: str
    include_router_prefix: str | None = None  # only for kind == "include_router"
    include_router_arg: str | None = None  # best-effort name of the router expression included
    kwargs: dict[str, str] = dataclasses.field(default_factory=dict)  # kwarg name -> ast.dump(value), any decorator kwarg


def classify_kwarg_change(old: "RouteRegistration", new: "RouteRegistration") -> str:
    """"none" | "openapi-only" | "behavioral", for two registrations already
    known to be the same route (same file + qualname)."""
    names = set(old.kwargs) | set(new.kwargs)
    changed = {n for n in names if old.kwargs.get(n) != new.kwargs.get(n)}
    if not changed:
        return "none"
    if changed <= OPENAPI_ONLY_KWARGS:
        return "openapi-only"
    return "behavioral"


def _receiver_name(node: ast.expr) -> tuple[str | None, bool]:
    """Return (name, is_simple) for the object a call/decorator hangs off."""
    if isinstance(node, ast.Name):
        return node.id, True
    if isinstance(node, ast.Attribute):
        # e.g. `items.router` -- report a dotted best-effort name but mark
        # not-simple, since resolving it requires cross-module knowledge.
        parts = []
        cur: ast.expr = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts)), False
        return None, False
    return None, False


def _literal_str(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _literal_str_list(node: ast.expr | None) -> tuple[str, ...] | None:
    if isinstance(node, (ast.List, ast.Tuple)):
        out = []
        for elt in node.elts:
            s = _literal_str(elt)
            if s is None:
                return None
            out.append(s)
        return tuple(out)
    return None


def _get_kwarg(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _qualname_of(fn: ast.FunctionDef | ast.AsyncFunctionDef, class_stack: list[str]) -> str:
    if class_stack:
        return ".".join(class_stack) + "." + fn.name
    return fn.name


def _decorator_first_line(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    if fn.decorator_list:
        return fn.decorator_list[0].lineno
    return fn.lineno


def parse_registrations(source: str, file: str) -> list[RouteRegistration]:
    tree = ast.parse(source, filename=file)
    out: list[RouteRegistration] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.class_stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.class_stack.append(node.name)
            self.generic_visit(node)
            self.class_stack.pop()

        def _visit_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                    continue
                verb = dec.func.attr
                if verb not in DECORATOR_VERBS:
                    continue
                receiver, is_simple = _receiver_name(dec.func.value)
                path_arg = dec.args[0] if dec.args else _get_kwarg(dec, "path")
                literal_path = _literal_str(path_arg)
                if verb == "api_route":
                    methods = _literal_str_list(_get_kwarg(dec, "methods")) or ("GET",)
                elif verb == "websocket":
                    methods = None
                else:
                    methods = (verb.upper(),)
                name_arg = _literal_str(_get_kwarg(dec, "name"))
                kwargs = {kw.arg: ast.dump(kw.value) for kw in dec.keywords if kw.arg is not None}
                out.append(
                    RouteRegistration(
                        kind="decorator",
                        receiver=receiver,
                        receiver_is_simple=is_simple,
                        verb=verb,
                        literal_path=literal_path,
                        methods=methods,
                        name_kwarg=name_arg,
                        kwargs=kwargs,
                        lineno=_decorator_first_line(node),
                        endpoint_qualname=_qualname_of(node, self.class_stack),
                        file=file,
                    )
                )
            self.generic_visit(node)

        visit_FunctionDef = _visit_func
        visit_AsyncFunctionDef = _visit_func

        def visit_Expr(self, node: ast.Expr) -> None:
            call = node.value
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                return
            attr = call.func.attr
            receiver, is_simple = _receiver_name(call.func.value)
            if attr in ("add_api_route", "add_route"):
                path_arg = call.args[0] if len(call.args) >= 1 else _get_kwarg(call, "path")
                endpoint_arg = call.args[1] if len(call.args) >= 2 else _get_kwarg(call, "endpoint")
                literal_path = _literal_str(path_arg)
                methods = _literal_str_list(_get_kwarg(call, "methods"))
                endpoint_qualname = None
                if isinstance(endpoint_arg, ast.Name):
                    # best-effort: only resolves a plain module-level function
                    # referenced by its bare name in the same file, which is
                    # also how inspect.getsourcelines/.__qualname__ will see
                    # it at runtime (rule 3's endpoint mapping) -- an
                    # imported name or a class attribute needs real name
                    # resolution the static pass doesn't attempt here.
                    endpoint_qualname = endpoint_arg.id
                name_arg = _literal_str(_get_kwarg(call, "name"))
                kwargs = {kw.arg: ast.dump(kw.value) for kw in call.keywords if kw.arg is not None}
                out.append(
                    RouteRegistration(
                        kind=attr,
                        receiver=receiver,
                        receiver_is_simple=is_simple,
                        verb=attr,
                        literal_path=literal_path,
                        methods=methods,
                        name_kwarg=name_arg,
                        kwargs=kwargs,
                        lineno=call.lineno,
                        endpoint_qualname=endpoint_qualname,
                        file=file,
                    )
                )
            elif attr == "include_router":
                router_arg = call.args[0] if call.args else _get_kwarg(call, "router")
                _, router_arg_repr = _receiver_name(router_arg) if router_arg is not None else (None, None)
                router_name, _ = _receiver_name(router_arg) if router_arg is not None else (None, False)
                prefix = _literal_str(_get_kwarg(call, "prefix")) or ""
                out.append(
                    RouteRegistration(
                        kind="include_router",
                        receiver=receiver,
                        receiver_is_simple=is_simple,
                        verb="include_router",
                        literal_path=None,
                        methods=None,
                        name_kwarg=None,
                        lineno=call.lineno,
                        endpoint_qualname=None,
                        file=file,
                        include_router_prefix=prefix,
                        include_router_arg=router_name,
                    )
                )
            self.generic_visit(node)

    Visitor().visit(tree)
    return out


# --------------------------------------------------------------------------
# Deriving a new route's full path from the OLD route table
# --------------------------------------------------------------------------




def full_path_for_new_route(
    old_table: list,
    reg: RouteRegistration,
    sibling_source_paths: dict[tuple[str, str], str],
) -> tuple[str | None, str]:
    """Return (full_path_or_None, reason). `sibling_source_paths` maps
    (file, endpoint_qualname) -> (receiver, that sibling's own literal
    decorator path), for every existing entry in old_table, so we can
    subtract the local path back out of the table's resolved path_format to
    recover the receiver's prefix.
    """
    if not reg.receiver_is_simple:
        return None, "receiver is not a simple name (e.g. `items.router`) -- can't scope siblings safely"
    if reg.literal_path is None:
        return None, "path is not a string literal -- can't derive statically"

    # Prefer a sibling registered through the *same* receiver variable --
    # one file can hold two routers with different prefixes (e.g. admin.py's
    # `router` and its nested `reports_router`), and grabbing any sibling in
    # the file would silently mix their prefixes up.
    sibling = None
    fallback_sibling = None
    for entry in old_table:
        if entry.endpoint_file != reg.file or entry.kind not in ("route", "websocket"):
            continue
        key = (entry.endpoint_file, entry.endpoint_qualname)
        info = sibling_source_paths.get(key)
        if info is None:
            continue
        sibling_receiver, local_path = info
        if fallback_sibling is None:
            fallback_sibling = (entry, local_path)
        if sibling_receiver == reg.receiver:
            sibling = (entry, local_path)
            break

    if sibling is None:
        sibling = fallback_sibling
    if sibling is None:
        return None, "no existing sibling route in this file to infer the receiver's prefix from"

    entry, local_path = sibling
    # entry.path is the fully-resolved, converter-preserving runtime path
    # (e.g. "/users/{user_id}/items/{item_id:int}"); local_path is that same
    # sibling's own literal decorator argument (e.g. "/{item_id:int}"), also
    # converter-preserving -- so this is an exact string suffix, no need to
    # go through compile_path (which would normalize ":int" away and make
    # the subtraction converter-blind).
    if local_path == "/":
        prefix = entry.path[:-1] if entry.path.endswith("/") else entry.path
    elif entry.path.endswith(local_path):
        prefix = entry.path[: -len(local_path)]
    else:
        return None, f"sibling's resolved path {entry.path!r} doesn't end with its own literal path {local_path!r} (unexpected)"

    return prefix + reg.literal_path, "ok"
