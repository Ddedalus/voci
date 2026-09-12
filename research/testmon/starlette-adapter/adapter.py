"""Prototype Starlette/FastAPI adapter for voci's affected-test selection.

Two independent pieces:

- Recorder: patches `starlette.routing.Router.__call__` once, at import time,
  to record (method, path) of every request/websocket-connect a test makes
  into whatever collector is current on a ContextVar.
- Route table: walks an app's final routes (after all `include_router`s,
  `Mount`s, etc. have run) and maps each endpoint back to (file, qualname,
  firstlineno) in source.
"""

from __future__ import annotations

import contextvars
import dataclasses
import inspect
from typing import Any, Callable, Iterable

import starlette.routing as _routing
from starlette.routing import Mount, Route, WebSocketRoute

# --------------------------------------------------------------------------
# Recorder
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Collector:
    name: str
    requests: set[tuple[str, str]] = dataclasses.field(default_factory=set)
    used_route_table: bool = False  # openapi()/url_path_for/etc were touched
    # (file, qualname, firstlineno) of every route the router matched for this
    # collector's requests -- recorded even when the endpoint body never ran
    # (422 body validation, a Depends() that raised, a plain 405).
    matched_routes: set[tuple[str | None, str | None, int | None]] = dataclasses.field(
        default_factory=set
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"Collector({self.name!r}, requests={sorted(self.requests)}, "
            f"used_route_table={self.used_route_table}, matched_routes={sorted(self.matched_routes, key=str)})"
        )


current_collector: contextvars.ContextVar[Collector | None] = contextvars.ContextVar(
    "voci_current_collector", default=None
)


class use_collector:
    """Context manager: `with use_collector(c): ...` records into `c`."""

    def __init__(self, collector: Collector | None) -> None:
        self.collector = collector
        self._token: Any = None

    def __enter__(self) -> Collector | None:
        self._token = current_collector.set(self.collector)
        return self.collector

    def __exit__(self, *exc: Any) -> None:
        current_collector.reset(self._token)


_orig_router_call: Callable = _routing.Router.__call__
_patched = False


async def _patched_router_call(self: _routing.Router, scope: dict, receive: Any, send: Any) -> None:
    is_outer = scope["type"] in ("http", "websocket") and not scope.get("_voci_recorded")
    if is_outer:
        scope["_voci_recorded"] = True
        collector = current_collector.get()
        if collector is not None:
            if scope["type"] == "websocket":
                method = "WEBSOCKET"
            else:
                method = scope.get("method", "?")
            collector.requests.add((method, scope["path"]))
    try:
        await _orig_router_call(self, scope, receive, send)
    finally:
        # scope is one shared dict mutated in place through every nested
        # Router/Mount call, so by the time the *outermost* call unwinds
        # (whether the handler ran, raised in a dependency, or raised in
        # body validation) it already carries whichever route matched --
        # Route.matches()/Router.app() set scope["endpoint"] on FULL *and*
        # PARTIAL (405) matches, before Route.handle() ever runs the
        # endpoint. FastAPI additionally sets scope["route"] (the APIRoute)
        # for its own routes. True 404s (Match.NONE everywhere) leave
        # neither key set -- there is no route to attribute to.
        if is_outer:
            collector = current_collector.get()
            if collector is not None:
                route_obj = scope.get("route")
                endpoint = route_obj.endpoint if route_obj is not None else scope.get("endpoint")
                if endpoint is not None:
                    collector.matched_routes.add(_map_endpoint(endpoint))


def install() -> None:
    """Monkeypatch Router.__call__. Idempotent."""
    global _patched
    if _patched:
        return
    _routing.Router.__call__ = _patched_router_call
    _patched = True


def uninstall() -> None:
    global _patched
    if not _patched:
        return
    _routing.Router.__call__ = _orig_router_call
    _patched = False


# --------------------------------------------------------------------------
# Route-table-introspection wrapping (openapi(), url_path_for / url_for)
# --------------------------------------------------------------------------


def _mark_used_route_table() -> None:
    collector = current_collector.get()
    if collector is not None:
        collector.used_route_table = True


def wrap_introspection(app: Any) -> None:
    """Wrap the handful of methods that depend on the *whole* route table."""
    # FastAPI: app.openapi() builds/caches the schema from every route.
    if hasattr(app, "openapi"):
        orig_openapi = app.openapi

        def openapi_wrapper(*a: Any, **kw: Any) -> Any:
            _mark_used_route_table()
            return orig_openapi(*a, **kw)

        app.openapi = openapi_wrapper

    # url_path_for on the app and on its router both walk every route.
    for holder in (app, getattr(app, "router", None)):
        if holder is None or not hasattr(holder, "url_path_for"):
            continue
        orig = holder.url_path_for

        def make_wrapper(orig: Callable) -> Callable:
            def wrapper(*a: Any, **kw: Any) -> Any:
                _mark_used_route_table()
                return orig(*a, **kw)

            return wrapper

        holder.url_path_for = make_wrapper(orig)


# --------------------------------------------------------------------------
# Route table extraction
# --------------------------------------------------------------------------


@dataclasses.dataclass
class RouteEntry:
    path: str  # raw, converters intact, e.g. "/users/{id:int}" -- for matching
    path_format: str  # display form, converters stripped, e.g. "/users/{id}"
    methods: tuple[str, ...] | None  # None for websocket routes
    name: str | None
    kind: str  # "route" | "websocket" | "mount-opaque"
    endpoint_file: str | None
    endpoint_qualname: str | None
    endpoint_firstlineno: int | None


_endpoint_map_cache: dict[int, tuple[str | None, str | None, int | None]] = {}


def _map_endpoint(endpoint: Callable) -> tuple[str | None, str | None, int | None]:
    # inspect.getsourcelines() re-reads and re-parses the whole source file
    # on every call (measured: dominates the per-request recording cost --
    # see scripts/bench_overhead2.py). It only needs to run once per
    # endpoint, ever, since a function's file/qualname/firstlineno can't
    # change without the function object itself changing (a fresh def/reload
    # produces a new object). Cached by id(); endpoint functions are held
    # alive by the route table anyway, so id() reuse after gc isn't a risk
    # for the app's own lifetime.
    key = id(endpoint)
    cached = _endpoint_map_cache.get(key)
    if cached is not None:
        return cached
    fn = endpoint
    # unwrap functools.partial / decorated callables where possible
    fn = inspect.unwrap(fn) if callable(fn) else fn
    try:
        file = inspect.getsourcefile(fn)
    except TypeError:
        file = None
    qualname = getattr(fn, "__qualname__", None)
    firstlineno = None
    if file is not None:
        try:
            _, firstlineno = inspect.getsourcelines(fn)
        except (OSError, TypeError):
            firstlineno = None
    result = (file, qualname, firstlineno)
    _endpoint_map_cache[key] = result
    return result


def extract_routes(app: Any, prefix: str = "") -> list[RouteEntry]:
    """Return the flattened, final route table for `app`.

    Uses fastapi.routing.iter_route_contexts when available (it resolves
    FastAPI's lazily-flattened include_router chains); falls back to a plain
    walk of Starlette's eagerly-flattened `.routes` otherwise. Recurses into
    Mounts whose sub-app exposes routes of its own.
    """
    out: list[RouteEntry] = []
    routes = getattr(app, "routes", None)
    if routes is None:
        routes = getattr(getattr(app, "router", None), "routes", [])

    try:
        from fastapi.routing import iter_route_contexts

        contexts = list(iter_route_contexts(routes))
    except ImportError:
        contexts = None

    def _resolved(rc: Any, original: Any, attr: str) -> Any:
        # RouteContext.path/.path_format/.methods/.name (and the
        # _EffectiveRouteContext they forward to) are only populated for the
        # plain-Route and APIRoute branches of _build_effective_context; the
        # Mount/WebSocketRoute/Host branches leave those convenience fields
        # at dataclass defaults ("" / None) and put the real, prefix-resolved
        # values on the nested `starlette_route` object instead. Try that
        # first, then rc's own fields, then the never-prefixed original.
        starlette_route = getattr(rc, "starlette_route", None)
        if starlette_route is not None:
            value = getattr(starlette_route, attr, None)
            if value:
                return value
        value = getattr(rc, attr, None)
        if value:
            return value
        return getattr(original, attr, None)

    if contexts is not None:
        for rc in contexts:
            original = rc.original_route
            if isinstance(original, Mount):
                sub_app = original.app
                resolved_path = _resolved(rc, original, "path") or ""
                mount_prefix = prefix + resolved_path.rstrip("/")
                if hasattr(sub_app, "routes") or hasattr(sub_app, "router"):
                    out.extend(extract_routes(sub_app, prefix=mount_prefix))
                else:
                    out.append(
                        RouteEntry(
                            path=mount_prefix + "/{path:path}",
                            path_format=mount_prefix + "/*",
                            methods=None,
                            name=original.name,
                            kind="mount-opaque",
                            endpoint_file=None,
                            endpoint_qualname=None,
                            endpoint_firstlineno=None,
                        )
                    )
                continue
            # rc.endpoint (via _EffectiveRouteContext.endpoint) is only
            # populated for the plain-Route branch of _build_effective_context;
            # APIRoute/APIWebSocketRoute/WebSocketRoute contexts leave it at
            # the dataclass default of None. The original route object always
            # carries its own endpoint regardless of prefix rewriting.
            endpoint = getattr(original, "endpoint", None) or rc.endpoint
            if endpoint is None:
                continue
            file, qualname, lineno = _map_endpoint(endpoint)
            kind = "websocket" if isinstance(original, WebSocketRoute) else "route"
            resolved_methods = _resolved(rc, original, "methods")
            methods = tuple(sorted(resolved_methods)) if resolved_methods else None
            # `path` keeps converter annotations (":int" etc); the prefix
            # threaded in from a Mount/include_router is itself already
            # converter-preserving (see mount_prefix above), so
            # concatenation stays exact for matching purposes.
            raw_path = _resolved(rc, original, "path") or ""
            path_format = _resolved(rc, original, "path_format") or raw_path
            out.append(
                RouteEntry(
                    path=prefix + raw_path,
                    path_format=prefix + path_format,
                    methods=methods,
                    name=_resolved(rc, original, "name"),
                    kind=kind,
                    endpoint_file=file,
                    endpoint_qualname=qualname,
                    endpoint_firstlineno=lineno,
                )
            )
        return out

    # Plain Starlette: no lazy include_router machinery. Routes are already
    # eagerly-flattened Route/WebSocketRoute/Mount objects.
    for route in routes:
        if isinstance(route, Mount):
            sub_app = route.app
            mount_prefix = prefix + route.path
            if hasattr(sub_app, "routes") or hasattr(sub_app, "router"):
                out.extend(extract_routes(sub_app, prefix=mount_prefix))
            else:
                out.append(
                    RouteEntry(
                        path=mount_prefix + "/{path:path}",
                        path_format=mount_prefix + "/*",
                        methods=None,
                        name=route.name,
                        kind="mount-opaque",
                        endpoint_file=None,
                        endpoint_qualname=None,
                        endpoint_firstlineno=None,
                    )
                )
            continue
        if isinstance(route, (Route, WebSocketRoute)):
            file, qualname, lineno = _map_endpoint(route.endpoint)
            kind = "websocket" if isinstance(route, WebSocketRoute) else "route"
            methods = tuple(sorted(route.methods)) if getattr(route, "methods", None) else None
            out.append(
                RouteEntry(
                    path=prefix + route.path,
                    path_format=prefix + route.path_format,
                    methods=methods,
                    name=route.name,
                    kind=kind,
                    endpoint_file=file,
                    endpoint_qualname=qualname,
                    endpoint_firstlineno=lineno,
                )
            )
    return out
