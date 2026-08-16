# The extractor: pytest collection as ground truth for migration codegen

Research notes for the pytest → velox migration tool (see
`docs/migration-problem-statement.md`, §4.1, §4.2, §4.3, §4.4, §8, §9, §11 Q1). Verified against
the pytest submodule at `pytest/` (9.2.0.dev, commit `28e86a6c2`) and empirically against the
installed pytest 9.1.1 in the project venv. All file/line references below are into
`pytest/src/_pytest/` unless marked "(9.1.1)".

## TL;DR

**The extractor approach is sound.** A ~120-line pytest plugin, run by the user with their own
environment as `pytest <suite> -p extractor --collect-only -q`, obtains — without executing a
single test — everything §4.1–§4.3, §8 and §9 need:

- **Per-test fixture resolution, already solved per test** (§4.1): `item._fixtureinfo` gives the
  parameter names, the transitive closure, and for every name in the closure the full ordered
  override chain of `FixtureDef`s. Nearest-wins is trivially readable (last element wins), and the
  *whole chain* is present, which is exactly what §4.2's specialization needs — including the
  "override requests its super fixture by the same name" pattern.
- **Every FixtureDef carries** scope, `params`/`ids`, autouse-ness, visibility (`baseid`/node),
  and the factory function object, from which module/qualname/file/lineno are recoverable through
  `functools.wraps` wrappers via pytest's own `get_real_func`.
- **Parametrization** (§4.2, §9): `item.callspec` holds params, per-arg indices, per-case marks
  from `pytest.param(marks=...)`, and — critically — pytest's exact generated id string
  (`callspec.id`), so emitting `ids=` verbatim is a straight copy. Direct vs indirect is
  distinguishable structurally (direct params resolve to a `DirectParamFixtureDef` sentinel in
  9.1+).
- **Autouse** (§4.3): enumerable globally as `{visibility nodeid: [fixture names]}` from the
  FixtureManager, and per-item as `initialnames - argnames - usefixtures`.
- **Config and plugins** (§8, pre-flight): all registered ini keys with resolved values, plus the
  active plugin list with distribution names and versions.

**What it cannot see** — each needs a separate static pass over source:

1. `request.getfixturevalue(...)` — confirmed invisible in `names_closure` (the `FuncFixtureInfo`
   docstring says so explicitly, and the experiment confirms it).
2. Test/fixture *bodies* — every §5 rename and §6 hazard is source-level work regardless.
3. `request.addfinalizer`, `request.config.getoption`, and the rest of §4.4's `request` surface.
4. Lazily-evaluated mark arguments: `skipif` string conditions are stored raw at collection and
   evaluated at setup (`skipping.py`) — which is what the codegen wants (the raw expression), but
   the extractor cannot tell you whether a condition would be true.
5. Anything conditional on the environment is resolved *as of the extraction run*. That is a
   feature (it is the ground truth for that env), but a suite whose fixtures differ per-platform
   needs one extraction per relevant env.

Answer to §11 open question 1: **yes, require a working pytest collection — but only of the
extractor, not of the migration tool.** The two halves decouple cleanly: the extractor is a
single-file plugin with no dependencies beyond pytest itself, runnable inside whatever container
or CI environment the suite already collects in, and its JSON dump is the only thing the codegen
(running anywhere, with no pytest requirement at all) consumes. The "only collects inside a
container" constraint reduces to "copy one JSON file out of the container".

## Proposed dump schema

Shaped by what the experiment produced; field names follow pytest's own where possible.

```jsonc
{
  "extractor_version": 1,
  "pytest_version": "9.1.1",
  "rootpath": "/abs/path",              // config.rootpath — nodeids are relative to this
  "inipath": "/abs/path/pyproject.toml", // or null
  "args": ["tests"],                     // config.args
  "ini": {                               // every registered ini key -> repr(resolved value)
    "strict_xfail": "False", "python_files": "['test_*.py', '*_test.py']", ...
  },
  "ini_aliases": {"xfail_strict": "strict_xfail"},   // parser._ini_aliases (9.1+)
  "plugins": {
    "distinfo": [{"dist": "pytest-asyncio", "version": "0.24.0", "plugin": "..."}],
    "names": ["...", "..."]              // pluginmanager.list_name_plugin() names
  },
  "autouse_by_node": {                   // visibility nodeid -> autouse fixture names, in order
    "": [], ".": ["root_autouse"], "integration": ["integ_autouse"]
  },
  "fixture_registry": {                  // ALL fixtures known to the session, incl. plugin ones
    "settings": [                        // ordered: registration order == outer -> inner
      {
        "argname": "settings",
        "scope": "session",              // FixtureDef.scope (public property, string)
        "params": null,                  // or list of repr(value)
        "ids": null,                     // repr of explicit ids tuple, or "<callable>"
        "autouse": false,                // FixtureDef._autouse (8.4+)
        "baseid": ".",                   // visibility prefix: "" = global, "." = rootdir conftest,
                                         // "integration" = that directory, "test_x.py" = module
        "kind": "FixtureDef",            // or "DirectParamFixtureDef"
        "argnames": ["..."],             // what the fixture itself requests (its Depends edges)
        "func": {
          "module": "conftest", "qualname": "settings",
          "file": "/abs/.../conftest.py", "lineno": 6,   // post-get_real_func, so real code
          "wrapped": false               // true if a decorator had to be unwrapped
        }
      },
      { "...": "integration/conftest.py override, later in the list" }
    ]
  },
  "items": [
    {
      "nodeid": "test_top.py::TestGroup::test_method",  // pytest's exact id, §9 verbatim source
      "path": "/abs/.../test_top.py",
      "originalname": "test_method",     // name without the [param] suffix
      "cls": "TestGroup",                // or null
      "argnames": ["settings"],          // real function parameters (fixture requests)
      "initialnames": ["root_autouse", "wrapped_fix", "settings"],  // autouse + usefixtures + args
      "names_closure": ["settings", "root_autouse"],    // transitive; excludes dynamic uses;
                                                         // may contain "request" (filter it)
      "name2fixturedefs": {              // name -> ordered chain, FURTHEST to CLOSEST;
        "settings": [ {"...": "root def"}, {"...": "integration override"} ]  // [-1] wins
      },
      "callspec": {                      // only present when parametrized (directly OR indirectly,
                                         // including via fixture params=)
        "id": "two",                     // exact suffix pytest puts in [...] — emit as ids=
        "params": {"n": "2"},            // argname -> repr(value)
        "indices": {"n": 1},             // argname -> param set index
        "marks": [{"name": "xfail", "args": [], "kwargs": {"reason": "'boom'"}}]  // pytest.param marks
      },
      "own_markers": [ {"name": "...", "args": [], "kwargs": {}} ],   // decorator + callspec marks
      "markers_with_origin": [           // iter_markers_with_node: nearest-first, with source node
        {"from": "test_top.py::TestGroup", "name": "classmark", "args": [], "kwargs": {}},
        {"from": "test_top.py", "name": "modmark", "args": [], "kwargs": {}}
      ]
    }
  ]
}
```

Values are `repr()`ed because params can be arbitrary objects; the codegen needs them only for
report text and for id emission (which uses `callspec.id`, already a string). The registry entry a
`name2fixturedefs` chain element refers to can be deduplicated against `fixture_registry` by
`(argname, baseid, func.file, func.lineno)` if dump size matters.

## API-by-API notes

### 1. `item._fixtureinfo` — per-test resolution (§4.1)

`FuncFixtureInfo`, `fixtures.py:451-499` (dataclass, slots: `argnames`, `initialnames`,
`names_closure`, `name2fixturedefs`). Built during collection in
`FixtureManager.getfixtureinfo()` (`fixtures.py:1808-1846`):
`initialnames = deduplicate_names(autousenames, usefixturesnames, argnames)` — autouse first,
matching pytest's setup ordering. Stored on the item in `Function.__init__`
(`python.py:1653` (9.1.1): `self._fixtureinfo: FuncFixtureInfo = fixtureinfo`) — i.e. at
collection time, unconditionally.

- **`argnames`**: the test function's own fixture-requesting parameters, extracted by
  `getfuncargnames` (`compat.py:108`) which unwraps decorators and strips `@mock.patch` args
  (`num_mock_patch_args`, `compat.py:89`).
- **`names_closure`**: transitive closure, DFS order then sorted by scope (widest first),
  `getfixtureclosure` at `fixtures.py:1947-2000`. Contains `"request"` when any fixture in the
  chain takes `request` — it has no `name2fixturedefs` entry; filter it.
- **`name2fixturedefs`**: the answer to §4.1. Docstring (`fixtures.py:479`): *"There may be
  multiple overriding fixtures with the same name. The sequence is ordered from furthest to
  closest to the function."* So the def a **test** gets is `chain[-1]` — nearest-wins is already
  resolved, per item. When an override requests its super fixture (same name in its own
  `argnames`), the super is `chain[-2]`, and so on down the stack —
  `traverse_fixture_closure` (`fixtures.py:402-447`) models exactly this with negative indices,
  and `pytest_generate_tests` (`fixtures.py:2002+`) walks `reversed(fixture_defs)` for the
  parametrized-super case. The codegen's §4.2 chain-specialization consumes the chain as-is.
- The closure **excludes** names claimed by direct parametrization
  (`_get_direct_parametrize_args`, `fixtures.py:1734`) — but 9.1+ then re-inserts a
  `DirectParamFixtureDef` for them (below), so the map stays total over `argnames`.

### 2. `FixtureDef` — what each definition carries

`fixtures.py:1111-1240` (class docstring: "only explicitly documented fields and methods are
considered public stable API"). Fields, all set in `__init__` and effectively final:

- `argname` — the name it is requested by (`name=` overrides already applied).
- `scope` — **public property**, string `"function" | "class" | "module" | "package" | "session"`
  (`fixtures.py:1134`).
- `params` / `ids` — for `@pytest.fixture(params=..., ids=...)`; `ids` may be a tuple or a
  callable (dump `"<callable>"` and fall back to per-item `callspec.id`, which is authoritative
  anyway).
- `_autouse` — private bool, present since **pytest 8.4.0** (added for a deprecation message;
  commit `6728ec560`). Cross-check against `autouse_by_node` if paranoid.
- `baseid` — visibility prefix: `""` = global (non-conftest plugin), `"."`-style directory ids for
  conftests, module/class nodeids for local fixtures. A fixture is visible to an item iff its
  baseid is a nodeid-prefix of the item. **Deprecated in 9.1** in favor of a `node` attribute
  (`FIXTURE_BASEID_DEPRECATED`, `fixtures.py:1136`; removal slated for pytest 10) — the extractor
  should read `fd.node.nodeid if fd.node is not None else fd.baseid` under a
  `warnings.catch_warnings` guard to stay quiet on both.
- `argnames` — the fixture's *own* requests (`fixtures.py:1185`), i.e. the fixture graph's edges.
- `func` — the factory. `get_real_func` (`compat.py:219`, `inspect.unwrap` + `functools.partial`
  peeling) recovers the real function; then `__module__`, `__qualname__`, `inspect.getfile`,
  `__code__.co_firstlineno` give the source location (pytest's own `getlocation`,
  `compat.py:75`, does the same). Verified to see through a `functools.wraps` decorator.
- There is **no unittest flag in 9.x** — the old `unittest=` parameter is gone; `unittest.py`
  handles TestCase fixtures separately. (A pre-9 extractor variant would read `fd.unittest`.)

Full registry: `session._fixturemanager._arg2fixturedefs` (private; `fixtures.py:1723` (9.1.1))
maps every fixture name known to the session — plugin, conftest, module, class — to its def list
in registration (outer→inner) order. This is the §4.5 placement input: `baseid` says which
conftest/module owns each def.

### 3. Parametrization — `item.callspec` (§4.2, §9)

`CallSpec` dataclass, `python.py:1157-1218` (named **`CallSpec2` in ≤9.1**, renamed in 9.2.dev
with a deprecated alias — commit `8c565f65f`; access via `item.callspec`, never by class name).
Present on the item **iff** the test is parametrized — directly, indirectly, *or* by a
`params=` fixture in its closure (`pytest_generate_tests` calls `metafunc.parametrize(...,
indirect=True)` for those, `fixtures.py:2002+`). Fields:

- `params` — argname → value, for **both** direct values and indirect/fixture params.
- `indices` — argname → parameter-set index. For a `params=` fixture this is the index into
  `FixtureDef.params`, which is how codegen aligns a test with a generated per-value fixture.
- `marks` — `pytest.param(..., marks=...)` marks for this case, also merged into
  `item.own_markers` (`python.py:1637-1639` (9.1.1)). Use `callspec.marks` to tell a per-case
  mark from a decorator mark; §5's "per-case xfail must split" rule keys off this.
- `id` (property) / `_idlist` — **pytest's exact generated id**. `python.py:510`:
  `subname = f"{name}[{callspec.id}]"` — the nodeid suffix is exactly `callspec.id`, so §9's
  "emit pytest's ids verbatim" is `ids=(callspec.id, ...)` copied per case, custom `id=`,
  `ids=` callables, float/bytes/tuple idmaking and `parametrize_long_str_id_strategy` (new ini in
  9.2.dev, commit `33ebdb1ab`) all already applied. Empirically: `[1]`, `[two]`, `[3.5]`,
  `[mysql]`, `[lite]`, `[pg]` all recovered verbatim.

**Direct vs indirect**: since 9.1 direct params are desugared into a registered
`DirectParamFixtureDef` (`python.py:1158-1182` (9.1.1), commit `6d03e6f74`), stored into the same
`name2fixturedefs` dict the item sees (`python.py:1405` (9.1.1):
`self._arg2fixturedefs[argname] = [fixturedef]` — `Metafunc._arg2fixturedefs` *is*
`fixtureinfo.name2fixturedefs`). So: a callspec argname whose chain is a `DirectParamFixtureDef`
(func is `get_direct_param_fixture_func`) is a **direct** param; a real `FixtureDef` means
**indirect** (or fixture `params=`, distinguished by that def's own `params` being non-None).
`Metafunc._params_directness` exists but dies with the metafunc; the structural check works from
the item alone. Pre-9.1 fallback: direct argnames simply have no real fixture def — check
`fd.func.__name__ == "get_direct_param_fixture_func"`.

### 4. Marks (§3, mechanical layer)

`Mark` (frozen dataclass: `name`, `args`, `kwargs`) in `mark/structures.py`. On items:

- `item.own_markers` (`nodes.py:190`) — decorator marks on the function (via
  `get_unpacked_marks`) **plus** callspec marks. Verified: `parametrize`, per-case `xfail`,
  `skipif`, `usefixtures` all appear.
- `item.iter_markers(name=)` / `item.iter_markers_with_node(name=)` (`nodes.py:330-348`,
  **documented public API**) — walks the node chain nearest-first, yielding `(node, mark)`; the
  node's `nodeid` tells you whether a mark is the item's own, from `pytest.param`, from the class
  (`TestGroup` pytestmark), or from the module (`pytestmark`). Verified: module `modmark` shows
  `from: "test_top.py"`, class `classmark` shows `from: "test_top.py::TestGroup"`.
- `skipif`/`xfail` conditions arrive raw (string or value) — evaluation happens at setup in
  `skipping.py`, not collection, so the extractor hands codegen the unevaluated expression, which
  is what §5's skipif row needs.

### 5. autouse (§4.3)

Two complementary views, both verified:

- **Global**: `FixtureManager._node_autousenames: dict[Node, list[str]]` (`fixtures.py:1724`
  (9.1.1), **9.1+**) plus legacy `_nodeid_autousenames: dict[str, list[str]]` (since **6.2**,
  commit `470ea504e`). Keyed by the visibility node: session (`""`), rootdir Directory (`"."`),
  subdirectory (`"integration"`), module, or class nodeid. This is exactly the `velox.use(...)`
  placement map: nodeid `"integration"` → declaration in `integration/__init__.py`. The extractor
  should merge both dicts (union, keyed by nodeid) to cover 6.2 → 9.x.
- **Per item**: `set(initialnames) - set(argnames) - usefixtures names` (autouse names come first
  in `initialnames`; `fixtures.py:1836`). Useful as a cross-check and for reporting which tests
  an autouse actually reaches.

`_getautousenames` walks `node.listchain()` (`fixtures.py:1930-1940`), so the per-item view
already honors visibility; codegen only needs the global map for placement.

### 6. Config and plugins (§8, pre-flight)

- `config.rootpath` / `config.inipath` / `config.args` — public.
- Resolved ini: iterate `config._parser._inidict` (all registered keys incl. plugin-added ones)
  through `config.getini(name)` (public, applies defaults and types; `config/__init__.py:1676`
  (9.1.1)). 56 keys dumped in the experiment. **Alias trap**: 9.1 renamed `xfail_strict` →
  `strict_xfail` (`skipping.py:40-46` (9.1.1), `aliases=["xfail_strict"]`); the parser's
  `_ini_aliases` map (`config/argparsing.py:59` (9.1.1)) should be dumped so codegen normalizes.
  `addopts` comes through `getini("addopts")` as the split argv list.
- Plugins: `config.pluginmanager.list_plugin_distinfo()` and `.list_name_plugin()` — **public
  pluggy API** (`pluggy/_manager.py:422,427`), giving `(dist name, version)` pairs for the §7
  "can I even migrate" gate. Conftest plugins appear in `names` by path.

### 7. Dynamic fixture use is invisible — confirmed

`FuncFixtureInfo` docstring, `fixtures.py:459-461`: *"An item may also request fixtures
dynamically (using `request.getfixturevalue`); these are not reflected here."* Empirically: a
fixture whose body calls `request.getfixturevalue("hidden")` produces a closure without
`hidden`. The migration tool therefore needs a separate static pass (AST grep for
`getfixturevalue`, `addfinalizer`, `request.` attribute uses) — the extractor dump tells it
*which* fixtures take `request` at all (the `"request"` entry in closures / fixture `argnames`),
narrowing where to look.

## Empirical results

Suite: two conftest levels (root: `settings`, `engine(settings)`, autouse `root_autouse`,
`backend(params=["sqlite","postgres"], ids=["lite","pg"])`, a `getfixturevalue` fixture, a
`functools.wraps`-wrapped fixture; `integration/`: `settings` override *requesting the super
`settings`*, autouse `integ_autouse`), tests exercising direct parametrize with
`pytest.param(2, id="two", marks=xfail)`, `indirect=True`, the `params=` fixture, `usefixtures`
+ `skipif`, a `TestGroup` class, module/class `pytestmark`. Run from the project root:

```
PYTHONPATH=<dir> EXTRACTOR_OUT=<dir>/dump.json uv run pytest <dir>/suite -p extractor --collect-only -q
```

Nine items collected, zero executed; `pytest_collection_finish` fired and the dump was complete.
Highlights, all matching the API reading:

- `integration/test_integration.py::test_engine`: `settings → [root def (baseid "."), integration
  def (baseid "integration")]` — chain order furthest→closest, winner last, super fixture
  reachable at `[-2]`. `engine` resolves to the single root def; its `argnames: ["settings"]`
  edge plus the item's chain is everything §4.2 needs to specialize `engine` for the subtree.
- `test_direct[two]`: `callspec.id == "two"`, `params {"n": "2"}`, `indices {"n": 1}`,
  `callspec.marks == [xfail(reason='boom')]`, and `n → DirectParamFixtureDef` (direct).
- `test_indirect[mysql]`: `backend →` the real conftest `FixtureDef` (indirect), value `'mysql'`
  in `callspec.params`.
- `test_params_fixture[lite]/[pg]`: a callspec **exists although the test has no parametrize
  mark** — fixture `params=` arrives as indirect parametrization with the fixture's `ids` applied
  (`lite`, `pg`) and `indices` into `FixtureDef.params`.
- `autouse_by_node`: `{"": [], ".": ["root_autouse"], "integration": ["integ_autouse"]}` — the
  `velox.use` placement map, directly.
- `test_uses`: `initialnames = [root_autouse, wrapped_fix, dyn]` (autouse first, then
  usefixtures, then argnames); `hidden` absent from the closure; `skipif` raw in `own_markers`;
  `markers_with_origin` attributes `modmark` to `test_top.py` and `classmark` to
  `test_top.py::TestGroup`.
- `wrapped_fix.func`: real inner function's file/lineno, `wrapped: true`.
- Registry: 34 names — ours plus builtins (`tmp_path`, `capsys`, `monkeypatch`, baseid `""`) plus
  installed-plugin fixtures (anyio's `free_tcp_port`, ...), each attributable to its distribution
  via the plugins dump. Ini dump: 56 resolved keys.

## Risks

- **Private API surface.** The extractor reads three genuinely private things:
  `item._fixtureinfo` (stable in shape for many years; `FuncFixtureInfo` fields unchanged),
  `FixtureManager._arg2fixturedefs` + `_node_autousenames`/`_nodeid_autousenames` (registry +
  autouse map), and `CallSpec._idlist`/`callspec.marks` (`callspec.id` and `params`/`indices` are
  de-facto stable — plugins like pytest-xdist and hypothesis depend on `item.callspec`).
  Everything else is public or documented: `iter_markers_with_node`, `FixtureDef.scope`,
  `getini`, pluggy's plugin listing, `nodeid`, `own_markers`.
- **Churn is real but survivable.** `fixtures.py` has ~139 commits since 2024. Recent moves that
  would have broken a naive extractor: `baseid` → `node` deprecation (9.1, removal in 10),
  `CallSpec2` → `CallSpec` rename (9.2.dev, alias kept), `_nodeid_autousenames` →
  `_node_autousenames` (9.1, both present), `xfail_strict` → `strict_xfail` ini alias (9.1),
  override-chain ordering by visibility (`7186cd465`), fixture visibility reworks
  (`ac4edb045`, `547eb1361`), new `pytest.register_fixture` (9.2.dev), new
  `parametrize_long_str_id_strategy` ini (9.2.dev). None changes the *information available*,
  only the attribute spelling — and the id strings the dump snapshots are immune to future
  id-generation changes because they are captured, not recomputed.
- **Mitigation: pin wide, shim narrow, version the dump.** Support `pytest>=8.4,<10` in one file
  (three or four `hasattr` shims: `_node_autousenames`, `DirectParamFixtureDef` presence,
  `fd.node` vs `fd.baseid`, `_ini_aliases`); pre-8.4 needs an autouse fallback and a
  direct-param heuristic (`func.__name__ == "get_direct_param_fixture_func"`) if ever needed.
  Because the codegen consumes only the JSON, pytest version risk is confined to the one small
  plugin the user runs, and `extractor_version`/`pytest_version` in the dump let the codegen
  refuse mismatches loudly.
- **Environment coupling.** The dump is ground truth for the env it ran in. Conditional fixture
  definitions, platform-dependent `pytest_plugins`, `pytest_generate_tests` reading options —
  all resolved as-of that run (correctly!). A suite that collects differently per platform needs
  per-env extraction and a codegen policy for diffs; the tool should record `sys.platform` and
  the plugin versions (it does) and warn.
- **Collection-only is genuinely complete.** `_fixtureinfo` and `callspec` are constructed in
  `Function.__init__` during collection; `pytest_collection_finish` runs under `--collect-only`
  after `pytest_collection_modifyitems` (so plugin-added marks/reordering are reflected). Nothing
  needed here materializes later: the only setup-time artifacts are fixture *values*
  (`cached_result`) and the request object's active-def stack, neither of which the dump wants.
  One prerequisite worth stating in the tool docs: collection imports every test module, so the
  suite's import-time side effects run — the same bar as `pytest --co` passing today.

## The extractor plugin (as validated)

```python
"""Collection-time ground-truth extractor: pytest plugin, dumps JSON, runs no tests."""

import inspect
import json
import os

from _pytest.compat import get_real_func


def _funcinfo(func):
    real = get_real_func(func)
    try:
        file = inspect.getfile(real)
        lineno = real.__code__.co_firstlineno
    except TypeError:
        file, lineno = None, None
    return {
        "module": getattr(real, "__module__", None),
        "qualname": getattr(real, "__qualname__", None),
        "file": file,
        "lineno": lineno,
        "wrapped": real is not func,
    }


def _fixturedef(fd):
    return {
        "argname": fd.argname,
        "scope": fd.scope,
        "params": None if fd.params is None else [repr(p) for p in fd.params],
        "ids": repr(fd.ids) if fd.ids is not None else None,
        "autouse": getattr(fd, "_autouse", False),
        "baseid": fd.baseid,
        "kind": type(fd).__name__,  # DirectParamFixtureDef => direct parametrize arg
        "argnames": list(fd.argnames),  # what this fixture itself requests
        "func": _funcinfo(fd.func),
    }


def _mark(m):
    return {"name": m.name, "args": [repr(a) for a in m.args],
            "kwargs": {k: repr(v) for k, v in m.kwargs.items()}}


def pytest_collection_finish(session):
    config = session.config
    fm = session._fixturemanager

    registry = {
        name: [_fixturedef(fd) for fd in defs]
        for name, defs in sorted(fm._arg2fixturedefs.items())
    }
    autouse = {
        node.nodeid: names for node, names in fm._node_autousenames.items()
    }

    ini = {}
    for name in sorted(config._parser._inidict):
        try:
            ini[name] = repr(config.getini(name))
        except Exception as exc:  # noqa: BLE001 - some inis need plugins/args
            ini[name] = f"<error: {exc}>"

    items = []
    for item in session.items:
        fi = getattr(item, "_fixtureinfo", None)
        callspec = getattr(item, "callspec", None)
        rec = {
            "nodeid": item.nodeid,
            "path": str(item.path),
            "originalname": getattr(item, "originalname", item.name),
            "cls": item.cls.__name__ if getattr(item, "cls", None) else None,
            "own_markers": [_mark(m) for m in item.own_markers],
            "markers_with_origin": [
                {"from": node.nodeid, **_mark(m)}
                for node, m in item.iter_markers_with_node()
            ],
        }
        if fi is not None:
            rec["argnames"] = list(fi.argnames)
            rec["initialnames"] = list(fi.initialnames)
            rec["names_closure"] = list(fi.names_closure)
            rec["name2fixturedefs"] = {
                name: [_fixturedef(fd) for fd in defs]
                for name, defs in fi.name2fixturedefs.items()
            }
        if callspec is not None:
            rec["callspec"] = {
                "id": callspec.id,
                "_idlist": list(callspec._idlist),
                "params": {k: repr(v) for k, v in callspec.params.items()},
                "indices": dict(callspec.indices),
                "marks": [_mark(m) for m in callspec.marks],
            }
        items.append(rec)

    dump = {
        "pytest_version": __import__("pytest").__version__,
        "rootpath": str(config.rootpath),
        "inipath": str(config.inipath) if config.inipath else None,
        "args": list(config.args),
        "addopts": repr(config.getini("addopts")),
        "ini": ini,
        "plugins": {
            "distinfo": [
                {"plugin": type(p).__name__ if not inspect.ismodule(p) else p.__name__,
                 "dist": d.project_name, "version": d.version}
                for p, d in config.pluginmanager.list_plugin_distinfo()
            ],
            "names": [name for name, _ in config.pluginmanager.list_name_plugin()],
        },
        "autouse_by_node": autouse,
        "fixture_registry": registry,
        "items": items,
    }
    out = os.environ.get("EXTRACTOR_OUT", "extractor-dump.json")
    with open(out, "w") as f:
        json.dump(dump, f, indent=1)
```

Production hardening not yet in this validated version: the `fd.node`/`fd.baseid` and
`_node_autousenames`/`_nodeid_autousenames` shims, `_ini_aliases` dump, `sys.platform`, a
`--extractor-out` CLI option via `pytest_addoption` instead of the env var, and registry
deduplication of `name2fixturedefs` entries by reference.
