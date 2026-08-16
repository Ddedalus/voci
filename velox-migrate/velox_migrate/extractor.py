"""The pytest plugin that dumps a suite's collection-time ground truth as JSON:

    pytest -p velox_migrate.extractor --collect-only -q --extractor-out ground-truth.json

Collection alone resolves fixture overrides, autouse visibility and parametrize ids for every
test, so every value here is read off objects pytest has already built rather than recomputed.

This module imports stdlib and pytest and nothing else, including nothing from `velox_migrate`.
That is what lets it be copied, as a single file, into an environment where the suite collects
and nothing else can be installed.
"""

from __future__ import annotations

import inspect
import json
import os
import platform
import sys

import pytest

# Bumped whenever the dump's shape changes. The loader refuses anything it does not equal, so a
# dump and the codegen reading it can never silently disagree about what a field means.
EXTRACTOR_VERSION = 1

# Below 8.4 there is no `FixtureDef._autouse`, so autouse-ness would have to be inferred from the
# visibility map alone; from 10 the deprecated `FixtureDef.baseid` is gone. Both are shimmable,
# neither is shimmed on speculation.
MIN_PYTEST = (8, 4)
MAX_PYTEST_EXCLUSIVE = (10,)

DEFAULT_OUT = os.path.join(".velox-migrate", "ground-truth.json")


def pytest_addoption(parser):
    group = parser.getgroup("velox-migrate")
    group.addoption(
        "--extractor-out",
        dest="extractor_out",
        default=None,
        metavar="PATH",
        help=(
            "Write the velox-migrate ground-truth dump here "
            f"(default: {DEFAULT_OUT}; the EXTRACTOR_OUT environment variable also works)."
        ),
    )


def pytest_configure(config):
    version = _pytest_version_tuple()
    if not (MIN_PYTEST <= version < MAX_PYTEST_EXCLUSIVE):
        supported = (
            f">={'.'.join(map(str, MIN_PYTEST))},<{'.'.join(map(str, MAX_PYTEST_EXCLUSIVE))}"
        )
        raise pytest.UsageError(
            f"velox-migrate's extractor supports pytest {supported}, "
            f"but this environment has {pytest.__version__}."
        )


def _pytest_version_tuple():
    parts = []
    for field in pytest.__version__.split(".")[:2]:
        digits = ""
        for char in field:
            if not char.isdigit():
                break
            digits += char
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


# --- unwrapping -------------------------------------------------------------------------------

try:
    from _pytest.compat import get_real_func as _get_real_func
except ImportError:  # pragma: no cover - only if pytest moves it within the supported range

    def _get_real_func(func):
        """Peel `functools.wraps` wrappers and `partial`s, as pytest's own helper does."""
        while True:
            unwrapped = inspect.unwrap(func)
            if unwrapped is not func:
                func = unwrapped
                continue
            if isinstance(func, __import__("functools").partial):
                func = func.func
                continue
            return func


# --- path normalization -----------------------------------------------------------------------


class _PathNormalizer:
    """Rewrites an absolute path to one that means the same thing on another machine.

    Paths under the suite become relative to rootdir. Paths belonging to the environment —
    pytest's own builtin fixtures, an installed plugin's fixtures — keep a `${prefix}` or
    `${base_prefix}` token standing in for the interpreter prefix. Anything else is left alone.
    """

    def __init__(self, rootpath):
        self._roots = [(str(rootpath), "")]
        # Longest prefix first, so a venv nested inside the suite wins over the suite itself.
        for token, prefix in (("${prefix}", sys.prefix), ("${base_prefix}", sys.base_prefix)):
            self._roots.append((prefix, token))
        self._roots.sort(key=lambda pair: len(pair[0]), reverse=True)

    def __call__(self, path):
        if path is None:
            return None
        text = str(path)
        for prefix, token in self._roots:
            if text == prefix:
                return token or "."
            if text.startswith(prefix + os.sep):
                tail = text[len(prefix) + 1 :].replace(os.sep, "/")
                return f"{token}/{tail}" if token else tail
        return text.replace(os.sep, "/")


# --- fixture defs -----------------------------------------------------------------------------


class _DefTable:
    """Assigns every `FixtureDef` a dump-local key.

    A test's override chain and the global registry both refer to the same definition objects,
    and a chain is repeated for every test that resolves through it. Emitting each definition
    once and referring to it by key keeps a large suite's dump proportional to its fixtures
    rather than to fixtures times tests.
    """

    def __init__(self, relpath):
        self._relpath = relpath
        self._keys = {}
        self._defs = {}
        # `id()` is only unique among live objects, so the table keeps every definition alive
        # for as long as it holds a key for it.
        self._alive = []

    def key(self, fixturedef):
        existing = self._keys.get(id(fixturedef))
        if existing is not None:
            return existing
        key = f"f{len(self._keys)}"
        self._keys[id(fixturedef)] = key
        self._alive.append(fixturedef)
        self._defs[key] = self._encode(fixturedef)
        return key

    def keys(self, fixturedefs):
        return [self.key(fd) for fd in fixturedefs]

    def as_dict(self):
        return self._defs

    def _encode(self, fd):
        func = self._funcinfo(fd.func)
        ids = getattr(fd, "ids", None)
        if ids is None:
            encoded_ids = None
        elif callable(ids):
            # A callable's repr carries its address, which would make the dump differ run to
            # run. The ids it would produce are already captured verbatim per test case.
            encoded_ids = "<callable>"
        else:
            encoded_ids = [repr(i) for i in ids]

        params = getattr(fd, "params", None)
        return {
            "argname": fd.argname,
            "scope": fd.scope,
            "params": None if params is None else [repr(p) for p in params],
            "ids": encoded_ids,
            # `_autouse` since 8.4. Cross-checkable against `autouse_by_node`.
            "autouse": bool(getattr(fd, "_autouse", False)),
            "visibility": _visibility(fd, func["file"]),
            "kind": type(fd).__name__,
            "direct_param": _is_direct_param(fd),
            "argnames": list(getattr(fd, "argnames", ())),
            "func": func,
        }

    def _funcinfo(self, func):
        real = _get_real_func(func)
        try:
            file = inspect.getfile(real)
        except TypeError:
            file = None
        code = getattr(real, "__code__", None)
        return {
            "module": getattr(real, "__module__", None),
            "qualname": getattr(real, "__qualname__", None),
            "file": self._relpath(file),
            "lineno": code.co_firstlineno if code is not None else None,
            "wrapped": real is not func,
        }


def _raw_visibility(fd):
    """The node a fixture is visible from, as pytest spells it.

    `baseid` is deprecated from 9.1 in favor of `node` and goes away in 10; `node` does not exist
    before 9.1. Both carry the same string.
    """
    node = getattr(fd, "node", None)
    if node is not None:
        return node.nodeid
    return getattr(fd, "baseid", "") or ""


def _visibility(fd, file):
    """The node a fixture is visible from, spelled the same way whatever pytest produced it.

    pytest 9.1 gives the rootdir conftest its own node, `"."`, and reserves `""` for a fixture a
    plugin registered globally. Earlier versions use `""` for both, so a conftest that lands
    there is re-keyed to the directory it was written in — recovering the distinction rather than
    discarding it, since where a fixture was defined decides where its replacement goes.
    """
    reported = _raw_visibility(fd)
    if reported:
        return reported
    if not file or os.path.isabs(file) or not file.endswith("conftest.py"):
        return reported
    return os.path.dirname(file) or "."


def _is_direct_param(fd):
    """Whether this "fixture" is really a directly-parametrized argument.

    Since 9.1 pytest desugars `@parametrize("n", ...)` into a `DirectParamFixtureDef` registered
    under `n`, so a direct argument and an indirect one look alike in the chain until the type
    is checked. Before that the same argument was backed by a shared function, which names it
    just as well.
    """
    if type(fd).__name__ == "DirectParamFixtureDef":
        return True
    return getattr(getattr(fd, "func", None), "__name__", None) == "get_direct_param_fixture_func"


def _mark(mark):
    return {
        "name": mark.name,
        "args": [repr(a) for a in mark.args],
        "kwargs": {k: repr(v) for k, v in mark.kwargs.items()},
    }


# --- session-level views ----------------------------------------------------------------------


def _autouse_by_node(fixturemanager, relpath):
    """Autouse fixture names keyed by the node they apply to — where a `velox.use` goes.

    pytest keeps two maps, one keyed by node and one by nodeid string, and unions them when it
    answers this question itself; so does this. Keys are then re-keyed through `_visibility` so
    that they agree with the fixtures they name.
    """
    merged = {}
    by_node = getattr(fixturemanager, "_node_autousenames", None) or {}
    for node, names in by_node.items():
        merged.setdefault(node.nodeid, []).extend(names)
    by_nodeid = getattr(fixturemanager, "_nodeid_autousenames", None) or {}
    for nodeid, names in by_nodeid.items():
        merged.setdefault(nodeid, []).extend(names)

    registry = getattr(fixturemanager, "_arg2fixturedefs", {})
    placed = {}
    for nodeid, names in merged.items():
        for name in names:
            placed.setdefault(_placement(registry, name, nodeid, relpath), []).append(name)
    return {nodeid: placed[nodeid] for nodeid in sorted(placed)}


def _placement(registry, name, nodeid, relpath):
    """Where the autouse fixture `name`, registered under `nodeid`, is normalized to."""
    for fd in registry.get(name, ()):
        if _raw_visibility(fd) == nodeid and getattr(fd, "_autouse", True):
            return _visibility(fd, relpath(_location(fd.func)))
    return nodeid


def _location(func):
    try:
        return inspect.getfile(_get_real_func(func))
    except TypeError:
        return None


def _ini(config):
    """Every registered ini key with its resolved value, plugin-contributed keys included."""
    parser = getattr(config, "_parser", None)
    inidict = getattr(parser, "_inidict", None) or {}
    resolved = {}
    for name in sorted(inidict):
        try:
            resolved[name] = repr(config.getini(name))
        # A few ini keys only resolve against arguments this run did not get, and one key that
        # cannot be read is not worth losing the rest of the dump over.
        except Exception as exc:
            resolved[name] = f"<error: {type(exc).__name__}: {exc}>"
    return resolved


def _plugins(config, relpath):
    manager = config.pluginmanager
    distinfo = [
        {
            "plugin": plugin.__name__ if inspect.ismodule(plugin) else type(plugin).__name__,
            "dist": dist.project_name,
            "version": dist.version,
        }
        for plugin, dist in manager.list_plugin_distinfo()
    ]
    distinfo.sort(key=lambda entry: (entry["dist"], entry["plugin"]))
    # A conftest is registered as a plugin under its own path, so these names are a mix of dotted
    # module names and filenames; normalizing is a no-op on the former. A plugin registered
    # without a name falls back to one built from its `id()`, which names nothing and differs
    # every run, so those are dropped.
    names = sorted(
        relpath(name) for name, _ in manager.list_name_plugin() if not str(name).isdigit()
    )
    return {"distinfo": distinfo, "names": names}


def _environment():
    """What the dump is ground truth *for*.

    A suite whose fixtures differ by platform or by plugin version resolves differently in each
    one, and the resulting dumps are each correct for their own environment. Recording the
    environment is what lets a later stage notice it is reading the wrong one.
    """
    return {
        "sys_platform": sys.platform,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
    }


def _item(item, table, relpath):
    record = {
        "nodeid": item.nodeid,
        "path": relpath(getattr(item, "path", None)),
        "lineno": item.location[1] if getattr(item, "location", None) else None,
        "originalname": getattr(item, "originalname", None) or item.name,
        "cls": item.cls.__name__ if getattr(item, "cls", None) else None,
        "own_markers": [_mark(m) for m in item.own_markers],
        "markers_with_origin": [
            {"from": node.nodeid, **_mark(mark)} for node, mark in item.iter_markers_with_node()
        ],
        # Emitted as real strings rather than left to be recovered from the mark's `repr`ed
        # arguments, and gathered the way pytest gathers them, so a `usefixtures` inherited from
        # the module or the class counts.
        "usefixtures": [
            name
            for _, mark in item.iter_markers_with_node(name="usefixtures")
            for name in mark.args
        ],
    }

    fixtureinfo = getattr(item, "_fixtureinfo", None)
    if fixtureinfo is not None:
        record["argnames"] = list(fixtureinfo.argnames)
        record["initialnames"] = list(fixtureinfo.initialnames)
        record["names_closure"] = list(fixtureinfo.names_closure)
        record["name2fixturedefs"] = {
            name: table.keys(defs) for name, defs in fixtureinfo.name2fixturedefs.items()
        }

    callspec = getattr(item, "callspec", None)
    if callspec is not None:
        record["callspec"] = {
            # pytest's own generated id, exactly as it appears between the brackets of the
            # nodeid. Captured rather than reproduced, so id-generation changes cannot move it.
            "id": callspec.id,
            "idlist": list(getattr(callspec, "_idlist", ())),
            "params": {k: repr(v) for k, v in callspec.params.items()},
            "indices": dict(callspec.indices),
            "marks": [_mark(m) for m in callspec.marks],
        }
    return record


def build_dump(session):
    """The whole dump, as plain JSON-ready data."""
    config = session.config
    fixturemanager = session._fixturemanager
    relpath = _PathNormalizer(config.rootpath)
    table = _DefTable(relpath)

    # Built before the items so that keys run outer-to-inner over the registry, which keeps a
    # diff between two dumps of a barely-changed suite readable.
    registry = {
        name: table.keys(defs)
        for name, defs in sorted(getattr(fixturemanager, "_arg2fixturedefs", {}).items())
    }
    items = [_item(item, table, relpath) for item in session.items]

    parser = getattr(config, "_parser", None)
    return {
        "extractor_version": EXTRACTOR_VERSION,
        "pytest_version": pytest.__version__,
        "environment": _environment(),
        "rootpath": str(config.rootpath),
        "inipath": relpath(config.inipath) if config.inipath else None,
        "args": list(config.args),
        "ini": _ini(config),
        # 9.1 renamed `xfail_strict` to `strict_xfail` and kept the old spelling as an alias.
        # Reading the alias map is how a later stage normalizes a suite's ini keys without
        # hardcoding pytest's rename history.
        "ini_aliases": dict(getattr(parser, "_ini_aliases", {}) or {}),
        "plugins": _plugins(config, relpath),
        "autouse_by_node": _autouse_by_node(fixturemanager, relpath),
        "fixture_defs": table.as_dict(),
        "fixture_registry": registry,
        "items": items,
    }


def _out_path(config):
    # Resolved against the directory pytest was invoked from, not rootdir, so that the path a
    # caller passes is the path they get.
    chosen = config.getoption("extractor_out", None) or os.environ.get("EXTRACTOR_OUT")
    return os.path.abspath(chosen or DEFAULT_OUT)


def pytest_collection_finish(session):
    # Collection-time, so `--collect-only` reaches here, and after
    # `pytest_collection_modifyitems`, so marks and deselections plugins applied are reflected.
    dump = build_dump(session)
    out = _out_path(session.config)
    parent = os.path.dirname(out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(dump, handle, indent=1, sort_keys=False)
        handle.write("\n")

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(
            f"velox-migrate: wrote ground truth for {len(dump['items'])} tests to {out}"
        )
