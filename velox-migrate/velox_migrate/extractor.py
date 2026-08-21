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
import re
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

# The exit statuses that mean collection reached the end: all passed, some test failed, and
# nothing was collected. Interrupted, internal error and usage error are the ones that leave a
# dump describing less than the suite. Mirrored by `schema.CLEAN_EXIT_STATUSES`, which this file
# cannot import.
CLEAN_EXIT_STATUSES = (0, 1, 5)


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
    # Cleared per run, so a second session in the same process does not inherit the first's
    # failures and get its dump refused for them.
    _collection_errors.clear()

    version = _pytest_version_tuple()
    if not (MIN_PYTEST <= version < MAX_PYTEST_EXCLUSIVE):
        supported = (
            f">={'.'.join(map(str, MIN_PYTEST))},<{'.'.join(map(str, MAX_PYTEST_EXCLUSIVE))}"
        )
        raise pytest.UsageError(
            f"velox-migrate's extractor supports pytest {supported}, "
            f"but this environment has {pytest.__version__}."
        )


# Nodeids whose collection failed. Module-level because pytest loads a plugin once per run, and
# the report hook that fills it has no session to hang it off.
_collection_errors = []


def pytest_collectreport(report):
    # A module that fails to import contributes no items, and a dump that did not say so would
    # describe a smaller suite as though it were the whole one.
    if report.failed:
        _collection_errors.append(report.nodeid or ".")


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
    pytest's own builtin fixtures, an installed plugin's fixtures — keep a `${site_packages}`,
    `${prefix}` or `${base_prefix}` token standing in for the directory they were installed
    into. Anything else is left alone.
    """

    def __init__(self, rootpath):
        self._roots = [(str(rootpath), "")]
        # An installed package is not reliably under `sys.prefix`: a runner that layers an
        # ephemeral environment over a base one puts what it installed on `sys.path` and leaves
        # the prefix pointing elsewhere. Every directory packages are imported from is therefore
        # a root in its own right, and one token covers them all, since which of an
        # environment's several package directories a file sits in says nothing about the suite.
        for entry in sys.path:
            if os.path.basename(entry) in ("site-packages", "dist-packages"):
                self._roots.append((entry, "${site_packages}"))
        for token, prefix in (("${prefix}", sys.prefix), ("${base_prefix}", sys.base_prefix)):
            self._roots.append((prefix, token))
        # Longest prefix first, so a venv nested inside the suite wins over the suite itself.
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

    def scrub(self, text):
        """`text` with any machine path inside it replaced, for messages rather than paths.

        Bounded at a separator or the end of the path, so a sibling directory that merely starts
        with the same characters is left alone.
        """
        for prefix, token in self._roots:
            text = re.sub(re.escape(prefix) + r"(?=[/\\]|$|[^\w./\\-])", token or ".", text)
        return text


# --- fixture defs -----------------------------------------------------------------------------


class _DefTable:
    """Assigns every `FixtureDef` a dump-local key.

    A test's override chain and the global registry both refer to the same definition objects,
    and a chain is repeated for every test that resolves through it. Emitting each definition
    once and referring to it by key keeps a large suite's dump proportional to its fixtures
    rather than to fixtures times tests.
    """

    def __init__(self, relpath, owners):
        self._relpath = relpath
        self._owners = owners
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
            "visibility": _visibility(fd, self._owners, self._relpath),
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


def _conftest_owners(config):
    """The conftest each object in a conftest's namespace came from, keyed by object identity.

    pytest registers a conftest as a plugin named by its path, so its module namespace is the
    record of which fixtures it contributed — including a fixture whose factory is a wrapper
    written in some other module, which its own location would misattribute.
    """
    plugins = list(config.pluginmanager.list_name_plugin())
    conftests = [
        (str(name), plugin)
        for name, plugin in plugins
        if inspect.ismodule(plugin) and str(name).endswith("conftest.py")
    ]

    # The very objects that plugins in their own right hold. A conftest re-exporting one of
    # their fixtures — `from pytest_asyncio.plugin import event_loop` — does not thereby own it.
    # Compared by identity rather than by `__module__`, which a decorator supplied by a plugin
    # stamps onto a factory the conftest really does define.
    plugin_owned = set()
    for name, plugin in plugins:
        if inspect.ismodule(plugin) and not str(name).endswith("conftest.py"):
            plugin_owned.update(_fixture_identities(vars(plugin).values()))

    owners = {}
    for name, plugin in conftests:
        for value in vars(plugin).values():
            for candidate in _candidates(value):
                if id(candidate) not in plugin_owned:
                    owners.setdefault(id(candidate), name)
    return owners


def _candidates(value):
    """`value` and the factory it holds.

    What `@pytest.fixture` leaves in a module is a definition object wrapping the factory, not
    the factory itself, so both spellings have to be recognizable.
    """
    return [
        candidate
        for candidate in (
            value,
            getattr(value, "_fixture_function", None),
            getattr(value, "__wrapped__", None),
        )
        if candidate is not None
    ]


def _fixture_identities(values):
    return {id(candidate) for value in values for candidate in _candidates(value)}


def _visibility(fd, owners, relpath):
    """The node a fixture is visible from, spelled the same way whatever pytest produced it.

    pytest 9.1 gives the rootdir conftest its own node, `"."`, and reserves `""` for a fixture a
    plugin registered globally. Earlier versions use `""` for both, so a fixture that lands there
    but belongs to a conftest is re-keyed to that conftest's directory — recovering the
    distinction rather than discarding it, since where a fixture is defined decides where its
    replacement goes.
    """
    reported = _raw_visibility(fd)
    if reported:
        return reported
    owner = owners.get(id(fd.func))
    if owner is None:
        return reported
    relative = relpath(owner)
    if not relative or os.path.isabs(relative):
        return reported
    return os.path.dirname(relative) or "."


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


def _autouse_by_node(fixturemanager):
    """Autouse fixture names keyed by the node they apply to — where a `velox.use` goes.

    pytest keeps two maps, one keyed by node and one by nodeid string, and unions them when it
    answers this question itself; so does this. The session node, spelled `""`, is folded into
    the rootdir, `"."`: both reach every collected test, and an ini-level `usefixtures` lands in
    the first while a rootdir conftest's autouse lands in the second.
    """
    merged = {}
    by_node = getattr(fixturemanager, "_node_autousenames", None) or {}
    for node, names in by_node.items():
        merged.setdefault(node.nodeid or ".", []).extend(names)
    by_nodeid = getattr(fixturemanager, "_nodeid_autousenames", None) or {}
    for nodeid, names in by_nodeid.items():
        merged.setdefault(nodeid or ".", []).extend(names)
    return {nodeid: _deduplicate(merged[nodeid]) for nodeid in sorted(merged)}


def _item_lineno(item):
    """Where a test is written, counting from one.

    `item.location` counts from zero, unlike every other line number in the dump, and a collector
    that is not reading Python has no line to report at all.
    """
    location = getattr(item, "location", None)
    lineno = location[1] if location else None
    if not isinstance(lineno, int) or lineno < 0:
        return None
    return lineno + 1


def _item_autouse(fixturemanager, item):
    """The autouse fixtures reaching one test, in the order pytest sets them up.

    Asked of pytest rather than derived by subtracting what the test requested from what it
    starts with: a test may also request an autouse fixture by name, and subtracting would drop
    exactly that one.
    """
    return _deduplicate(list(fixturemanager._getautousenames(item)))


def _deduplicate(names):
    """`names` without repeats, in the order pytest would set them up."""
    seen = set()
    return [name for name in names if not (name in seen or seen.add(name))]


def _ini_repr(value, relpath):
    """`value` as source text, with any path it holds made portable.

    pytest resolves a `paths`-typed ini key to absolute paths, which would otherwise be the one
    part of a dump that still names the machine it was taken on.
    """
    if isinstance(value, os.PathLike):
        return repr(relpath(os.fspath(value)))
    if isinstance(value, (list, tuple)):
        inner = ", ".join(_ini_repr(item, relpath) for item in value)
        if isinstance(value, tuple):
            return f"({inner},)" if len(value) == 1 else f"({inner})"
        return f"[{inner}]"
    return repr(value)


def _ini(config, relpath):
    """Every registered ini key with its resolved value, plugin-contributed keys included."""
    parser = getattr(config, "_parser", None)
    inidict = getattr(parser, "_inidict", None) or {}
    resolved = {}
    for name in sorted(inidict):
        try:
            resolved[name] = _ini_repr(config.getini(name), relpath)
        # A few ini keys only resolve against arguments this run did not get, and one key that
        # cannot be read is not worth losing the rest of the dump over.
        except Exception as exc:
            resolved[name] = relpath.scrub(f"<error: {type(exc).__name__}: {exc}>")
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


def _item(item, table, relpath, fixturemanager):
    record = {
        "nodeid": item.nodeid,
        "path": relpath(getattr(item, "path", None)),
        "lineno": _item_lineno(item),
        "originalname": getattr(item, "originalname", None) or item.name,
        "cls": item.cls.__qualname__ if getattr(item, "cls", None) else None,
        "own_markers": [_mark(m) for m in item.own_markers],
        "markers_with_origin": [
            {"from": node.nodeid, **_mark(mark)} for node, mark in item.iter_markers_with_node()
        ],
        # Emitted as real strings rather than left to be recovered from the mark's `repr`ed
        # arguments, and gathered the way pytest gathers them, so a `usefixtures` inherited from
        # the module or the class counts.
        "usefixtures": _deduplicate(
            name
            for _, mark in item.iter_markers_with_node(name="usefixtures")
            for name in mark.args
        ),
    }

    record["autouse"] = _item_autouse(fixturemanager, item)

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


def build_dump(session, exitstatus=0):
    """The whole dump, as plain JSON-ready data."""
    config = session.config
    fixturemanager = session._fixturemanager
    relpath = _PathNormalizer(config.rootpath)
    table = _DefTable(relpath, _conftest_owners(config))

    # Built before the items so that keys run outer-to-inner over the registry, which keeps a
    # diff between two dumps of a barely-changed suite readable.
    registry = {
        name: table.keys(defs)
        for name, defs in sorted(getattr(fixturemanager, "_arg2fixturedefs", {}).items())
    }
    items = [_item(item, table, relpath, fixturemanager) for item in getattr(session, "items", ())]

    parser = getattr(config, "_parser", None)
    return {
        "extractor_version": EXTRACTOR_VERSION,
        "pytest_version": pytest.__version__,
        "environment": _environment(),
        "rootpath": str(config.rootpath),
        "inipath": relpath(config.inipath) if config.inipath else None,
        "args": [relpath(arg) for arg in config.args],
        "ini": _ini(config, relpath),
        # 9.1 renamed `xfail_strict` to `strict_xfail` and kept the old spelling as an alias.
        # Reading the alias map is how a later stage normalizes a suite's ini keys without
        # hardcoding pytest's rename history.
        "ini_aliases": dict(getattr(parser, "_ini_aliases", {}) or {}),
        # What pytest itself made of the run. Anything but a clean collection means the dump
        # describes less than the suite.
        "exit_status": int(exitstatus),
        "collection_errors": sorted(set(_collection_errors)),
        "plugins": _plugins(config, relpath),
        "autouse_by_node": _autouse_by_node(fixturemanager),
        "fixture_defs": table.as_dict(),
        "fixture_registry": registry,
        "items": items,
    }


def _out_path(config):
    # Resolved against the directory pytest was invoked from, not rootdir, so that the path a
    # caller passes is the path they get.
    chosen = config.getoption("extractor_out", None) or os.environ.get("EXTRACTOR_OUT")
    return os.path.abspath(chosen or DEFAULT_OUT)


def pytest_sessionfinish(session, exitstatus):
    # Not `pytest_collection_finish`, which also runs when pytest is about to fail with a usage
    # error — a mistyped path there produces a clean dump describing no tests at all. By here
    # the session's own verdict is known and travels with the dump.
    dump = build_dump(session, exitstatus)
    out = _out_path(session.config)
    parent = os.path.dirname(out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(dump, handle, indent=1, sort_keys=False)
        handle.write("\n")

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        if exitstatus in CLEAN_EXIT_STATUSES:
            note = f"wrote ground truth for {len(dump['items'])} tests to {out}"
        else:
            note = (
                f"pytest exited {exitstatus} before finishing collection, so {out} describes "
                "less than the suite and will be refused"
            )
        reporter.write_line(f"velox-migrate: {note}")
