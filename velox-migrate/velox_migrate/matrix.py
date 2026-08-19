"""The support matrix: every pytest construct a suite can contain, and what becomes of it.

One row per construct, keyed by a code (`VX214`) that stays with it from this table into the
audit's findings, into a `VELOX-TODO[category]` marker in converted source, and into the
rewrite rule that performs the translation — so a report line, a marker and a rule are the same
row seen from three places.

A row's `disposition` is the whole classification: `mechanical` needs nobody's attention,
`marker` converts with a caveat recorded in the source, `refused` and `unsupported` do not
convert, and `hazard` is a behaviour change that survives conversion because concurrency, not
syntax, is what changed. Two constructs that differ in disposition are two rows, so a lookup by
code never has to be qualified by context.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum


class Disposition(StrEnum):
    """What the migration does with a construct."""

    MECHANICAL = "mechanical"
    MARKER = "marker"
    REFUSED = "refused"
    UNSUPPORTED = "unsupported"
    HAZARD = "hazard"


class Area(StrEnum):
    """The part of a suite a construct belongs to, which is also how a report groups it."""

    WIRING = "wiring"
    MARKS = "marks"
    BODIES = "bodies"
    CONFIG = "config"
    HAZARDS = "hazards"


# Codes are blocked by area, and the block is what `Construct.area` reads.
_AREA_BY_BLOCK = {
    "0": Area.WIRING,
    "1": Area.MARKS,
    "2": Area.BODIES,
    "3": Area.CONFIG,
    "4": Area.HAZARDS,
}


@dataclass(frozen=True, slots=True)
class Construct:
    """One row of the support matrix.

    `serialized` says a test carrying this construct ends up running alone — it is what the
    percent-of-suite-serialized estimate counts, and it is independent of the disposition, since a
    construct can be mechanical to translate and still cost concurrency. `detected` is false for
    the handful of constructs no dump and no parse can see; those are named in a report's
    blind-spot section instead of ever appearing as a finding. `suite_level` is true for a
    construct that belongs to the suite rather than to any test, so its findings name no tests and
    it never counts against a test's own classification.
    """

    code: str
    subject: str
    target: str | None
    disposition: Disposition
    marker: str | None
    note: str
    action: str | None = None
    serialized: bool = False
    detected: bool = True
    suite_level: bool = False

    @property
    def area(self) -> Area:
        return _AREA_BY_BLOCK[self.code[2]]

    @property
    def converts(self) -> bool:
        """Whether conversion produces working code for this construct."""
        return self.disposition in (Disposition.MECHANICAL, Disposition.MARKER)


def _row(
    code: str,
    subject: str,
    disposition: Disposition,
    note: str,
    *,
    target: str | None = None,
    marker: str | None = None,
    action: str | None = None,
    serialized: bool = False,
    detected: bool = True,
    suite_level: bool = False,
) -> Construct:
    return Construct(
        code=code,
        subject=subject,
        target=target,
        disposition=disposition,
        marker=marker,
        note=note,
        action=action,
        serialized=serialized,
        detected=detected,
        suite_level=suite_level,
    )


MECHANICAL = Disposition.MECHANICAL
MARKER = Disposition.MARKER
REFUSED = Disposition.REFUSED
UNSUPPORTED = Disposition.UNSUPPORTED
HAZARD = Disposition.HAZARD


CONSTRUCTS: tuple[Construct, ...] = (
    # --- wiring ------------------------------------------------------------------------------
    _row(
        "VX001",
        "@pytest.fixture",
        MECHANICAL,
        "A fixture becomes a module-level object, and every parameter that requested it by name "
        "becomes a `Depends()` default naming the import.",
        target="@velox.fixture()",
    ),
    _row(
        "VX002",
        "@pytest.fixture(params=...)",
        MECHANICAL,
        "The fixture keeps its cases, and `request.param` becomes the factory's own `param` "
        "argument.",
        target="@velox.fixture(params=...)",
    ),
    _row(
        "VX003",
        'fixture scope="class" or scope="package"',
        MARKER,
        "velox scopes are call, function, module and session. A class-scoped fixture becomes "
        "module-scoped and a package-scoped one becomes session-scoped, so each is shared by more "
        "tests than it was.",
        target="@velox.fixture(scope=...)",
        marker="scope",
        action="Confirm the fixture tolerates being shared more widely, or split it.",
    ),
    _row(
        "VX004",
        "conftest.py fixture",
        MECHANICAL,
        "Every fixture in a `conftest.py` moves to a `fixtures.py` beside it, and the tests that "
        "used it import it from there.",
        target="fixtures.py beside the conftest",
    ),
    _row(
        "VX005",
        "conftest fixture overriding one from a parent directory",
        MECHANICAL,
        "The override and every fixture between it and the tests that reach it are generated as a "
        "specialized chain named for the overriding directory.",
        target="a specialized fixture chain",
    ),
    _row(
        "VX006",
        "conftest override whose chain exceeds the specialization budget",
        REFUSED,
        "Specializing this override would duplicate more fixtures than the budget allows, which "
        "is the point where a generated chain stops being reviewable.",
        action="Unwind the override into an explicit seam or a parametrized fixture before "
        "converting.",
    ),
    _row(
        "VX007",
        "@pytest.mark.parametrize(..., indirect=True)",
        MECHANICAL,
        "Each value used becomes its own fixture object, since the case is chosen at the call "
        "site rather than by the fixture.",
        target="one generated fixture per value",
    ),
    _row(
        "VX008",
        "@pytest.fixture(autouse=True)",
        MECHANICAL,
        "Becomes one `velox.use(...)` declaration on the module or package the fixture covered, "
        "rather than a parameter on each test.",
        target="velox.use(...)",
    ),
    _row(
        "VX009",
        "@pytest.mark.usefixtures on a module or class",
        MECHANICAL,
        "Becomes a `velox.use(...)` declaration covering the same tests.",
        target="velox.use(...)",
    ),
    _row(
        "VX010",
        "@pytest.mark.usefixtures on some of a module's tests",
        MARKER,
        "`velox.use(...)` declares a fixture for a whole module, so the fixture reaches the "
        "module's other tests too.",
        target="velox.use(...)",
        marker="usefixtures",
        action="Confirm the other tests in the module tolerate the fixture, or move them out.",
    ),
    _row(
        "VX011",
        'request.getfixturevalue("literal")',
        MECHANICAL,
        "A literal name is a static dependency, so it becomes an ordinary `Depends()` parameter.",
        target="a Depends() parameter",
    ),
    _row(
        "VX012",
        "request.getfixturevalue(computed)",
        REFUSED,
        "The name is decided at run time, so nothing static can say which fixture is meant.",
        action="Replace the computed lookup with explicit dependencies, or with a factory "
        "fixture that takes them.",
    ),
    _row(
        "VX013",
        "request.addfinalizer(fn)",
        MECHANICAL,
        "An unconditional finalizer becomes the fixture's `yield` teardown.",
        target="yield teardown",
    ),
    _row(
        "VX014",
        "request.addfinalizer under a condition",
        REFUSED,
        "A `yield` fixture always runs its teardown, so a conditionally registered finalizer "
        "would start running where it did not.",
        action="Make the finalizer unconditional, moving the condition inside it.",
    ),
    _row(
        "VX015",
        "request.node, request.config, request.cls, request.instance, request.fixturenames",
        MARKER,
        "`velox.test_info` carries the test's id, tags, timeout and worker. Anything else these "
        "reach — a node's own marks, ini values, the owning class — has no counterpart.",
        target="velox.test_info",
        marker="request",
        action="Rewrite the use against `test_info`, or pass what the fixture needs in.",
    ),
    _row(
        "VX016",
        "request.config.getoption on a custom pytest_addoption flag",
        UNSUPPORTED,
        "The flag exists because a conftest hook added it to pytest's command line, and velox has "
        "no plugin command-line surface for it to be added to.",
        action="Read the value from the environment or from configuration instead.",
    ),
    _row(
        "VX017",
        "request passed to another function or held past setup",
        REFUSED,
        "The uses cannot be enumerated at the call site, so no per-use translation applies.",
        action="Narrow the fixture to the values it actually reads from `request`.",
    ),
    _row(
        "VX018",
        "class Test* grouping",
        MECHANICAL,
        "The class stays as namespacing and each method collects as "
        "`path.py::TestGroup::test_name`.",
        target="class Test*",
    ),
    _row(
        "VX019",
        "setup_method, teardown_method, setup_class, setup_function",
        REFUSED,
        "velox builds a fresh instance per test and calls no setup protocol, so these never run.",
        action="Move each one's body into a fixture the class declares with `velox.use(...)`.",
    ),
    _row(
        "VX020",
        "unittest.TestCase",
        UNSUPPORTED,
        "velox collects functions and `class Test*` methods; a `TestCase` subclass brings its own "
        "lifecycle, assertions and skipping.",
        action="Rewrite the class as plain test functions.",
    ),
    _row(
        "VX021",
        "doctests",
        UNSUPPORTED,
        "velox collects test modules, not docstrings.",
        action="Keep running doctests under their own command.",
    ),
    _row(
        "VX022",
        "conftest hook (pytest_configure, pytest_collection_modifyitems, ...)",
        UNSUPPORTED,
        "Hooks are how a pytest plugin reaches into collection and reporting, and velox has no "
        "hook protocol.",
        marker=None,
        action="Decide per hook: fixtures replace setup hooks, and reporting hooks have no "
        "counterpart.",
        suite_level=True,
    ),
    _row(
        "VX023",
        "pytest_addoption",
        UNSUPPORTED,
        "velox's command line is fixed, so a suite cannot add a flag to it.",
        action="Read the setting from the environment or from `[tool.velox]`'s `env`.",
        suite_level=True,
    ),
    _row(
        "VX024",
        "pytest_generate_tests",
        MARKER,
        "The cases the hook produced are in the dump, ids included, so they convert to an "
        "explicit `@velox.parametrize` — one that lists what this extraction saw and generates "
        "nothing new.",
        target="@velox.parametrize with the generated cases",
        marker="generated-params",
        action="Confirm the frozen case list is what the suite should keep testing.",
    ),
    _row(
        "VX025",
        "pytest_plugins in a conftest",
        MARKER,
        "Fixtures the named module contributed are translated from the dump like any other, and "
        "the declaration itself goes away.",
        marker="plugin-module",
        action="Check the named module for hooks and non-fixture content, which do not travel.",
        suite_level=True,
    ),
    _row(
        "VX026",
        "a fixture requesting request for its own parametrization",
        MECHANICAL,
        "`request.param` in a `params=` fixture becomes the factory's `param` argument.",
        target="the param argument",
    ),
    _row(
        "VX027",
        "conftest override reaching an autouse fixture",
        REFUSED,
        "A specialized chain gives the subtree its own objects, and a `velox.use(...)` declares "
        "one of them for a directory — so an autouse fixture the override changes would be "
        "declared twice over the same tests, once for each definition.",
        action="Request the fixture by name where it is needed, or unwind the override.",
    ),
    _row(
        "VX030",
        "a fixture an installed plugin provides",
        UNSUPPORTED,
        "The fixture lives in a distribution, not in the suite, so there is no source to move and "
        "nothing registers it under velox.",
        action="Write the fixture into the suite, or drop the tests that need it.",
    ),
    # --- marks and parametrization -------------------------------------------------------------
    _row(
        "VX101",
        "@pytest.mark.parametrize",
        MECHANICAL,
        "Becomes `@velox.parametrize` carrying pytest's own case ids verbatim, so node ids do not "
        "move.",
        target="@velox.parametrize",
    ),
    _row(
        "VX102",
        "pytest.param(..., marks=...)",
        REFUSED,
        "Marks apply to a whole test in velox, so a mark on one case has nowhere to go.",
        action="Split the case into its own test, or branch on it inside the body.",
    ),
    _row(
        "VX103",
        "@pytest.mark.skipif with a string condition",
        MARKER,
        "velox takes a bool or a zero-arg callable. A string condition is evaluated by pytest at "
        "collection, and a callable is evaluated by velox at run time.",
        target="@velox.skipif",
        marker="skipif-string",
        action="Confirm the condition reads the same at run time as it did at collection.",
    ),
    _row(
        "VX104",
        "@pytest.mark.skip, @pytest.mark.skipif(bool)",
        MECHANICAL,
        "Same decorator, same reason.",
        target="@velox.skip, @velox.skipif",
    ),
    _row(
        "VX105",
        "@pytest.mark.xfail with a condition",
        REFUSED,
        "`@velox.xfail` applies unconditionally, so a conditional expectation has to become a "
        "branch someone chooses.",
        action="Split the conditional cases, or assert the two outcomes explicitly.",
    ),
    _row(
        "VX106",
        "@pytest.mark.xfail(run=False)",
        MARKER,
        "Nothing in velox marks a test as expected-to-fail without running it, so it becomes a "
        "skip and is reported as one.",
        target="@velox.skip",
        marker="xfail-run",
        action="Confirm a skip is the outcome the suite wants reported.",
    ),
    _row(
        "VX107",
        "xfail_strict",
        MECHANICAL,
        "The ini setting is written into each generated `@velox.xfail` as `strict=`.",
        target="strict= on each xfail",
        suite_level=True,
    ),
    _row(
        "VX108",
        "@pytest.mark.filterwarnings",
        UNSUPPORTED,
        "Warning filters are process-global, and velox runs tests concurrently in one process, so "
        "a per-test filter cannot be honoured.",
        action="Move the filter into the test body with `warnings.catch_warnings`, and mark the "
        "test `@velox.solo`.",
        serialized=True,
    ),
    _row(
        "VX109",
        "a custom mark",
        MECHANICAL,
        "Becomes `@velox.tag(...)`, selectable with `-m`, and needs no registration.",
        target="@velox.tag(...)",
    ),
    _row(
        "VX110",
        "@pytest.mark.asyncio, @pytest.mark.anyio, event_loop fixtures",
        MECHANICAL,
        "velox runs `async def` tests itself, so the mark and the loop fixtures go away. A backend "
        "parametrization the mark carried goes with it, and with it that segment of the test's id.",
        target="deleted",
    ),
    _row(
        "VX111",
        "@pytest.mark.timeout",
        MECHANICAL,
        "Becomes `@velox.timeout(...)`.",
        target="@velox.timeout(...)",
    ),
    _row(
        "VX112",
        "a mark an installed plugin acts on",
        UNSUPPORTED,
        "The behaviour was the plugin's, and velox records marks as tags without acting on them.",
        action="Replace the mark's effect with a fixture, or drop the tests that need it.",
    ),
    _row(
        "VX113",
        "two skip, xfail or timeout marks on one test",
        REFUSED,
        "velox raises when a scalar mark is applied twice, so this is a collection error rather "
        "than a last-one-wins.",
        action="Keep one, having decided which.",
    ),
    _row(
        "VX114",
        "a case id composed from more than one axis",
        MARKER,
        "pytest builds one id per case out of every axis that varies it — stacked `parametrize` "
        "marks, or a mark over a `params=` fixture — so no single mark can carry the ids "
        "verbatim, and velox composes its own from the values.",
        target="@velox.parametrize without ids",
        marker="case-ids",
        action="Check any CI configuration, dashboard or `--last-failed` habit that names these "
        "ids, and write an explicit `ids=` where one matters.",
    ),
    _row(
        "VX115",
        "pytestmark on a module or a class",
        MECHANICAL,
        "velox reads marks from the test function, and a mark on a class is a collection error, so "
        "each mark the assignment applied becomes a decorator on every test it reached.",
        target="the same mark on each test",
    ),
    # --- bodies and builtin fixtures -----------------------------------------------------------
    _row(
        "VX201",
        "capsys",
        MECHANICAL,
        "Becomes the `velox.capture` fixture, whose `.out` and `.err` read the live buffers.",
        target="velox.capture",
    ),
    _row(
        "VX202",
        "capsys.readouterr() more than once in a body",
        MARKER,
        "`readouterr()` returns a snapshot and clears the buffer; `capture.out` is cumulative and "
        "never cleared, so a second read sees the first read's output too.",
        target="velox.capture",
        marker="capture",
        action="Compare against the text captured so far, or slice off what was already asserted.",
    ),
    _row(
        "VX203",
        "capfd, capsysbinary, capfdbinary",
        UNSUPPORTED,
        "velox captures by replacing `sys.stdout` and `sys.stderr`, so writes to file descriptor "
        "1 by a subprocess or a C extension are not captured, and there is no binary variant.",
        action="Redirect the subprocess to a file the test reads, or drop the assertion.",
    ),
    _row(
        "VX204",
        "caplog.records, caplog.messages",
        MECHANICAL,
        "Become the same attributes on `velox.log_records`.",
        target="velox.log_records",
    ),
    _row(
        "VX205",
        "caplog.set_level(...)",
        MARKER,
        "`LogRecords.set_level(...)` returns a context manager and does nothing until it is "
        "entered, so the statement becomes a `with` block around the rest of the test. Logger "
        "levels are process-global.",
        target="with log_records.set_level(...)",
        marker="caplog-level",
        action="Wrap the part of the test that needs the level, and mark the test `@velox.solo`.",
        serialized=True,
    ),
    _row(
        "VX206",
        "caplog.text, caplog.record_tuples, caplog.clear()",
        MECHANICAL,
        "Become the same names on `velox.log_records`, which formats `.text` from its own "
        "records on every read rather than keeping a second stream.",
        target="velox.log_records",
    ),
    _row(
        "VX207",
        "tmp_path, tmp_path_factory",
        MECHANICAL,
        "Same names, same `pathlib.Path`.",
        target="tmp_path, tmp_path_factory",
    ),
    _row(
        "VX208",
        "tmpdir, tmpdir_factory",
        REFUSED,
        "These are `py.path.local` objects with their own API — `.join`, `.strpath`, string "
        "division — so the body has to change, not the name.",
        action="Move the suite to `tmp_path` under pytest first; the bodies stay verified while "
        "you do.",
    ),
    _row(
        "VX209",
        "pytest.raises(E, match=...)",
        MECHANICAL,
        "Becomes `velox.raises`, matching with `re.search` as pytest does.",
        target="velox.raises",
    ),
    _row(
        "VX210",
        "pytest.raises(E, func, *args)",
        MECHANICAL,
        "Becomes `velox.raises`, which has the same callable form: it calls `func(*args, "
        "**kwargs)` under the hood and returns the resulting `ExceptionInfo`. Left unconverted "
        "when `match=` is among the kwargs: pytest forwards it to `func` there, but "
        "`velox.raises` always intercepts it.",
        target="velox.raises",
    ),
    _row(
        "VX211",
        "pytest.raises(asyncio.CancelledError)",
        UNSUPPORTED,
        "Cancellation is how velox enforces timeouts, so `velox.raises` refuses to swallow it.",
        action="Assert on the cancellation's effect instead of catching it.",
    ),
    _row(
        "VX212",
        "pytest.approx(scalar)",
        MECHANICAL,
        "Becomes `velox.approx`.",
        target="velox.approx",
    ),
    _row(
        "VX213",
        "pytest.approx over a list, tuple, or dict",
        MECHANICAL,
        "Becomes `velox.approx`, which compares a list or tuple elementwise by position and a "
        "dict elementwise by key, all under the same tolerances. Left unconverted where a list, "
        "tuple, dict, or set is nested one level inside it: `velox.approx` only walks one level, "
        "so there is no position to compare a nested container by.",
        target="velox.approx",
    ),
    _row(
        "VX214",
        "pytest.skip(), pytest.fail(), pytest.xfail() as a statement",
        REFUSED,
        "`velox.skip` is a decorator factory: called in a body it builds a decorator, discards it "
        "and lets the test run on. Renaming this would turn a conditional skip into a silent pass.",
        action="Lift the condition into `@velox.skipif`, or make the test assert what it means.",
    ),
    _row(
        "VX215",
        'pytest.importorskip("mod")',
        UNSUPPORTED,
        "There is no imperative skip to reach for at import time.",
        action="Guard the import at module level and put `@velox.skipif` on the tests.",
    ),
    _row(
        "VX216",
        "pytest.warns, recwarn, pytest.deprecated_call",
        UNSUPPORTED,
        "Recording warnings means installing a process-global filter, which cannot be scoped to "
        "one of several tests in flight.",
        action="Assert on what the warning accompanies, or catch it inside a `@velox.solo` test.",
        serialized=True,
    ),
    _row(
        "VX217",
        "unittest.mock.patch as a decorator",
        MECHANICAL,
        "The patch stays as it is and velox schedules the test alone. Injected parameters are "
        "emitted after the mock arguments the decorator fills positionally.",
        target="@mock.patch, scheduled solo",
        serialized=True,
    ),
    _row(
        "VX218",
        "unittest.mock.patch as a context manager",
        MECHANICAL,
        "A patch entered inside a body is invisible until it runs, so the test carries "
        "`@velox.solo` and nothing else runs while it holds.",
        target="@velox.solo on the test",
        serialized=True,
    ),
    _row(
        "VX219",
        "mocker (pytest-mock)",
        MARKER,
        "`mocker.patch` is `mock.patch` with teardown attached to the fixture, so each call "
        "becomes a `mock.patch` the test enters, and the test runs alone.",
        target="mock.patch plus @velox.solo",
        marker="mocker",
        action="Rewrite each `mocker.` call as the `mock` call it wraps.",
        serialized=True,
    ),
    _row(
        "VX220",
        "pytestconfig, cache, record_property, pytester and other pytest builtins",
        UNSUPPORTED,
        "These fixtures expose pytest's own configuration, cache and self-test machinery.",
        action="Drop the use, or read the value from configuration.",
    ),
    _row(
        "VX221",
        "pytest.approx over a set or a generator expression",
        UNSUPPORTED,
        "Neither has a position to compare by: a set is unordered, and a generator is spent after "
        "one read. A numpy array is left unconverted here too, since velox has no numpy "
        "dependency to compare it with.",
        action="Compare a sorted sequence instead of a set, or a list instead of a generator.",
    ),
    _row(
        "VX222",
        "caplog.handler, caplog.get_records(...)",
        UNSUPPORTED,
        "velox has no handler object behind `velox.log_records`, and no per-phase record split "
        "for `get_records` to read.",
        action="Assert on `records` or `messages` directly instead of `handler`; there is no "
        "phase-scoped equivalent for `get_records`.",
    ),
    # --- configuration and plugins -------------------------------------------------------------
    _row(
        "VX301",
        "testpaths",
        MECHANICAL,
        "Same meaning under `[tool.velox]`.",
        target="testpaths",
        suite_level=True,
    ),
    _row(
        "VX302",
        "python_files",
        MECHANICAL,
        "Becomes `test_file_patterns`.",
        target="test_file_patterns",
        suite_level=True,
    ),
    _row(
        "VX303",
        "norecursedirs",
        MECHANICAL,
        "Becomes `ignore`.",
        target="ignore",
        suite_level=True,
    ),
    _row(
        "VX304",
        "pytest-timeout's timeout setting",
        MECHANICAL,
        "Becomes the `timeout` key, which velox applies per test.",
        target="timeout",
        suite_level=True,
    ),
    _row(
        "VX305",
        "addopts",
        MARKER,
        "Each flag needs its own answer: some have a velox spelling, some were a plugin's, and "
        "unknown `[tool.velox]` keys are a hard error, so nothing speculative is written.",
        marker="addopts",
        action="Translate the flags you rely on into `[tool.velox]` or into the CI invocation.",
        suite_level=True,
    ),
    _row(
        "VX306",
        "markers, asyncio_mode, and other settings with nothing to configure",
        MECHANICAL,
        "velox tags need no registration and it runs async tests without being told to, so these "
        "settings have no counterpart to write.",
        target="dropped",
        suite_level=True,
    ),
    _row(
        "VX307",
        "filterwarnings",
        UNSUPPORTED,
        "A suite-wide warning filter has no `[tool.velox]` key, and the warnings module is "
        "process-global.",
        action="Set the filter in a session fixture if it is global, or per test with "
        "`warnings.catch_warnings`.",
        suite_level=True,
    ),
    _row(
        "VX308",
        "log_cli, console_output_style, required_plugins and other reporting settings",
        UNSUPPORTED,
        "These configure pytest's terminal and plugin machinery.",
        action="Drop them, or reach for the closest velox flag.",
        suite_level=True,
    ),
    _row(
        "VX309",
        "an ini setting a plugin registered",
        UNSUPPORTED,
        "The setting exists because a plugin asked pytest for it, and unknown `[tool.velox]` keys "
        "are a hard error.",
        action="Drop it with the plugin, or move the value into the environment.",
        suite_level=True,
    ),
    _row(
        "VX320",
        "an installed plugin migration deletes",
        MECHANICAL,
        "Its whole job is done by the runner, so the suite loses the dependency.",
        target="deleted",
        suite_level=True,
    ),
    _row(
        "VX321",
        "an installed plugin with a translation",
        MECHANICAL,
        "What the suite uses it for has a velox spelling, reached through the rows for the "
        "fixtures and marks themselves.",
        suite_level=True,
    ),
    _row(
        "VX322",
        "an installed plugin that mutates process-global state",
        HAZARD,
        "It works, and what it changes is visible to every test running at the same time.",
        action="Schedule the tests that use it alone, or isolate them.",
        serialized=True,
        suite_level=True,
    ),
    _row(
        "VX323",
        "an installed plugin with no velox path",
        UNSUPPORTED,
        "Nothing in velox provides what it does.",
        action="Keep those tests under pytest, or drop the plugin's use.",
        suite_level=True,
    ),
    # --- hazards -------------------------------------------------------------------------------
    _row(
        "VX401",
        "monkeypatch",
        HAZARD,
        "Every `monkeypatch` call changes state the whole process shares — an attribute, an "
        "environment variable, `sys.path`, the working directory — which a serial runner made "
        "private to the running test.",
        action="Inject the dependency instead, or mark the test `@velox.solo`; `chdir` needs "
        "`@velox.isolated`.",
        serialized=True,
    ),
    _row(
        "VX402",
        "os.environ writes",
        HAZARD,
        "The environment is process-wide, so a variable one test sets is set for every test in "
        "flight.",
        action="Set suite-wide values in `[tool.velox]`'s `env`, and mark tests that need their "
        "own value `@velox.solo`.",
        serialized=True,
    ),
    _row(
        "VX403",
        "warnings filter mutation",
        HAZARD,
        "`simplefilter` and `filterwarnings` write a process-global list, and `catch_warnings` "
        "restores it wholesale on exit — including changes another test made meanwhile.",
        action="Mark the test `@velox.solo`.",
        serialized=True,
    ),
    _row(
        "VX404",
        "logging level or handler mutation",
        HAZARD,
        "Logger objects are process-global, so a level a test raises is raised for everything "
        "logging at the same time.",
        action="Mark the test `@velox.solo`, or assert on records without changing levels.",
        serialized=True,
    ),
    _row(
        "VX405",
        "sys.modules surgery or importlib.reload",
        HAZARD,
        "Replacing or reloading a module rebinds it for every test that has already imported it.",
        action="Mark the test `@velox.isolated`.",
        serialized=True,
    ),
    _row(
        "VX406",
        "os.chdir",
        HAZARD,
        "The working directory belongs to the process, and a relative path elsewhere in the suite "
        "resolves against whatever it currently is.",
        action="Use absolute paths, or mark the test `@velox.isolated`.",
        serialized=True,
    ),
    _row(
        "VX407",
        "locale, decimal context or interpreter limits",
        HAZARD,
        "These are interpreter-wide settings with no per-task scope.",
        action="Mark the test `@velox.isolated`.",
        serialized=True,
    ),
    _row(
        "VX408",
        "a globally patched clock (freezegun, time-machine)",
        HAZARD,
        "Freezing time replaces the clock for the whole process, so every test in flight sees the "
        "frozen time.",
        action="Inject a clock, or mark the test `@velox.solo`.",
        serialized=True,
    ),
    _row(
        "VX409",
        "asyncio.run, get_event_loop, new_event_loop, run_until_complete",
        HAZARD,
        "velox runs the test on a loop that is already running, and starting a second loop from "
        "inside it fails.",
        action="Await the coroutine directly in an `async def` test.",
    ),
    _row(
        "VX410",
        "a blocking call in an async body",
        HAZARD,
        "One blocking call in an `async def` holds the loop, and every other test in flight waits "
        "for it. A sync `def` test is safe: it runs on an executor thread and holds only its own "
        "slot.",
        action="Use the async client, or make the test a plain `def`.",
    ),
    _row(
        "VX411",
        "a module-level or class-level global a test writes",
        HAZARD,
        "State that outlives a test is shared with every test that reads it, and nothing "
        "sequences them any more.",
        action="Move the state into a fixture.",
    ),
    _row(
        "VX412",
        "seeded randomness and sequence counters",
        HAZARD,
        "A seed set in a fixture, or a factory sequence, produced deterministic values because "
        "tests drew from it one at a time.",
        action="Seed per test, or assert on shape rather than on generated values.",
    ),
    _row(
        "VX413",
        "a fixed external resource",
        HAZARD,
        "A hardcoded port, path or database worked because one test used it at a time.",
        action="Allocate per test, or mark the tests that share it `exclusive=` on their fixture.",
    ),
    _row(
        "VX414",
        "test-order dependence",
        HAZARD,
        "A test that passes only because something earlier in the file ran first has nothing in "
        "its source saying so.",
        action="Run the suite shuffled under pytest before converting.",
        detected=False,
    ),
    _row(
        "VX415",
        "fixture teardown timing",
        HAZARD,
        "pytest tears a module-scoped fixture down when the module's last test finishes; velox "
        "tears it down when its last holder releases, with other modules still running.",
        action="Check anything that asserts a teardown has happened.",
        detected=False,
    ),
    _row(
        "VX416",
        "shared state behind application code",
        HAZARD,
        "Singleton caches, `functools.lru_cache` on application functions, ORM identity maps and "
        "module-level registries are shared by tests that never mention them.",
        action="Reset them in a fixture, or inject them.",
        detected=False,
    ),
)


BY_CODE: Mapping[str, Construct] = {construct.code: construct for construct in CONSTRUCTS}

# Marker categories, which are also the `VELOX-TODO[category]` spellings converted source carries.
MARKER_CATEGORIES: tuple[str, ...] = tuple(
    dict.fromkeys(construct.marker for construct in CONSTRUCTS if construct.marker)
)


@dataclass(frozen=True, slots=True)
class PluginRecipe:
    """What migration makes of one installed pytest plugin.

    `code` is the matrix row that classifies it, so a plugin's disposition and a construct's are
    read the same way.
    """

    dist: str
    code: str
    note: str

    @property
    def construct(self) -> Construct:
        return BY_CODE[self.code]


# Keyed by distribution name, as `GroundTruth.plugins` reports it. A plugin absent from this table
# is classified `VX323` — unknown means no recipe, which is the honest answer for a plugin nobody
# has looked at.
PLUGINS: Mapping[str, PluginRecipe] = {
    recipe.dist: recipe
    for recipe in (
        PluginRecipe("pytest-asyncio", "VX320", "velox runs async tests itself."),
        PluginRecipe("anyio", "VX320", "velox runs async tests itself."),
        PluginRecipe("pytest-trio", "VX323", "velox runs tests on asyncio."),
        PluginRecipe("pytest-tornasync", "VX323", "velox runs tests on asyncio."),
        PluginRecipe(
            "pytest-xdist",
            "VX320",
            "velox runs tests concurrently in one process, so there are no workers to distribute "
            "across. Its flags live on in CI invocations.",
        ),
        PluginRecipe(
            "pytest-randomly",
            "VX320",
            "Ordering is not velox's to shuffle. A suite that ran green under it is evidence the "
            "suite does not depend on order.",
        ),
        PluginRecipe("pytest-timeout", "VX320", "`@velox.timeout(...)` and the `timeout` setting."),
        PluginRecipe("pytest-env", "VX320", "`[tool.velox]`'s `env` sets the suite's environment."),
        PluginRecipe(
            "pytest-mock",
            "VX321",
            "`mocker` becomes `mock.patch`, and the tests that use it run alone.",
        ),
        PluginRecipe(
            "pytest-httpx", "VX321", "Transport-level patching, per test, which stays as it is."
        ),
        PluginRecipe("respx", "VX321", "Transport-level patching, per test, which stays as it is."),
        PluginRecipe(
            "pytest-recording", "VX321", "Transport-level patching, per test, which stays as it is."
        ),
        PluginRecipe(
            "pytest-vcr", "VX321", "Transport-level patching, per test, which stays as it is."
        ),
        PluginRecipe(
            "pytest-freezegun", "VX322", "It patches the process clock for the running test."
        ),
        PluginRecipe(
            "pytest-freezer", "VX322", "It patches the process clock for the running test."
        ),
        PluginRecipe(
            "pytest-time-machine", "VX322", "It patches the process clock for the running test."
        ),
        PluginRecipe(
            "factory-boy",
            "VX322",
            "Factory sequences are class-level counters shared by every test drawing from them.",
        ),
        PluginRecipe(
            "pytest-django",
            "VX322",
            "Its database fixtures translate through the fixture rows, and its per-test "
            "transaction and settings overrides are process-global.",
        ),
        PluginRecipe(
            "pytest-flask",
            "VX322",
            "Its app and client fixtures translate through the fixture rows.",
        ),
        PluginRecipe(
            "pytest-postgresql",
            "VX322",
            "Its database fixtures translate through the fixture rows; the database itself is a "
            "shared resource.",
        ),
        PluginRecipe(
            "pytest-cov",
            "VX323",
            "Coverage of a concurrent single-process run is measured by running `coverage` around "
            "velox, not by a plugin.",
        ),
        PluginRecipe("pytest-subtests", "VX323", "Nothing in velox reports sub-results."),
        PluginRecipe(
            "pytest-benchmark",
            "VX323",
            "A timing measurement taken while other tests run means nothing.",
        ),
        PluginRecipe("pytest-repeat", "VX323", "Nothing in velox repeats a test."),
        PluginRecipe("pytest-rerunfailures", "VX323", "Nothing in velox retries a test."),
        PluginRecipe("pytest-flakefinder", "VX323", "Nothing in velox repeats a test."),
        PluginRecipe(
            "hypothesis",
            "VX323",
            "`@given` rewrites a test's signature, which is where "
            "velox reads injected dependencies from.",
        ),
        PluginRecipe(
            "pytest-bdd", "VX323", "Its step decorators build tests through pytest hooks."
        ),
        PluginRecipe("pytest-splinter", "VX323", "Nothing in velox provides browser fixtures."),
    )
}


# pytest's own fixtures, each keyed to the row that classifies it. A fixture a test requests that
# is neither here, nor defined in the suite, came from an installed plugin — which is `VX030`.
BUILTIN_FIXTURES: Mapping[str, str] = {
    "request": "VX026",
    "tmp_path": "VX207",
    "tmp_path_factory": "VX207",
    "tmpdir": "VX208",
    "tmpdir_factory": "VX208",
    "capsys": "VX201",
    "caplog": "VX204",
    "capfd": "VX203",
    "capsysbinary": "VX203",
    "capfdbinary": "VX203",
    "capteesys": "VX203",
    "monkeypatch": "VX401",
    "recwarn": "VX216",
    "pytestconfig": "VX220",
    "cache": "VX220",
    "record_property": "VX220",
    "record_testsuite_property": "VX220",
    "record_xml_attribute": "VX220",
    "pytester": "VX220",
    "testdir": "VX220",
    "doctest_namespace": "VX021",
}

# pytest's own ini settings, each keyed to the row that classifies it, plus the one a plugin
# registers that velox has a key for. A setting a suite writes that is not here belongs to a
# plugin, which is `VX309`.
INI_SETTINGS: Mapping[str, str] = {
    "testpaths": "VX301",
    # pytest-timeout's, and the one plugin setting `[tool.velox]` has a home for, so it is
    # classified by what becomes of it rather than by who registered it.
    "timeout": "VX304",
    "python_files": "VX302",
    "python_classes": "VX302",
    "python_functions": "VX302",
    "norecursedirs": "VX303",
    "addopts": "VX305",
    "markers": "VX306",
    "minversion": "VX306",
    "empty_parameter_set_mark": "VX306",
    "xfail_strict": "VX107",
    "strict_xfail": "VX107",
    "filterwarnings": "VX307",
    "usefixtures": "VX009",
    "consider_namespace_packages": "VX306",
    "pythonpath": "VX308",
    "required_plugins": "VX308",
    "console_output_style": "VX308",
    "junit_suite_name": "VX308",
    "junit_logging": "VX308",
    "junit_log_passing_tests": "VX308",
    "junit_duration_report": "VX308",
    "junit_family": "VX308",
    "cache_dir": "VX308",
    "verbosity_assertions": "VX308",
    "verbosity_test_cases": "VX308",
    "tmp_path_retention_count": "VX308",
    "tmp_path_retention_policy": "VX308",
    "faulthandler_timeout": "VX308",
    "doctest_optionflags": "VX021",
    "doctest_encoding": "VX021",
    "log_cli": "VX308",
    "log_cli_level": "VX308",
    "log_cli_format": "VX308",
    "log_cli_date_format": "VX308",
    "log_level": "VX308",
    "log_format": "VX308",
    "log_date_format": "VX308",
    "log_file": "VX308",
    "log_file_level": "VX308",
    "log_file_format": "VX308",
    "log_file_date_format": "VX308",
    "log_auto_indent": "VX308",
    "truncation_limit_lines": "VX308",
    "truncation_limit_chars": "VX308",
}

# Marks an installed plugin acts on, keyed to the distribution that acts on them. A mark here is
# `VX112`: velox records marks as tags and nothing acts on them.
PLUGIN_MARKS: Mapping[str, str] = {
    "django_db": "pytest-django",
    "urls": "pytest-django",
    "ignore_template_errors": "pytest-django",
    "freeze_time": "pytest-freezegun",
    "repeat": "pytest-repeat",
    "flaky": "pytest-rerunfailures",
    "vcr": "pytest-vcr",
    "default_cassette": "pytest-recording",
    "block_network": "pytest-recording",
    "allow_hosts": "pytest-recording",
    "respx": "respx",
    "httpx_mock": "pytest-httpx",
    "benchmark": "pytest-benchmark",
    "subtests": "pytest-subtests",
}

# Marks pytest itself, or the runner, answers — so a mark outside this set and outside
# `PLUGIN_MARKS` is one of the suite's own tags.
KNOWN_MARKS: frozenset[str] = frozenset(
    {
        "parametrize",
        "skip",
        "skipif",
        "xfail",
        "usefixtures",
        "filterwarnings",
        "timeout",
        "asyncio",
        "anyio",
        "trio",
        "tryfirst",
        "trylast",
        "hookwrapper",
    }
)


def construct(code: str) -> Construct:
    """The matrix row `code` names. Raises `KeyError` for a code the matrix does not define."""
    try:
        return BY_CODE[code]
    except KeyError:
        raise KeyError(f"No support-matrix row is coded {code!r}.") from None


def plugin(dist: str) -> PluginRecipe:
    """What migration makes of the plugin distributed as `dist`, unknown ones included."""
    known = PLUGINS.get(dist)
    if known is not None:
        return known
    return PluginRecipe(dist, "VX323", "velox-migrate has no recipe for this plugin.")


def by_area() -> Iterator[tuple[Area, tuple[Construct, ...]]]:
    """Every area with its rows, in code order."""
    for area in Area:
        yield area, tuple(c for c in CONSTRUCTS if c.area is area)


def _validate() -> None:
    """Guard the table's own invariants, which nothing else in the pipeline re-checks."""
    seen: set[str] = set()
    for c in CONSTRUCTS:
        if c.code in seen:
            raise AssertionError(f"Support-matrix code {c.code} is used twice.")
        seen.add(c.code)
        if c.code[:2] != "VX" or not c.code[2:].isdigit() or c.code[2] not in _AREA_BY_BLOCK:
            raise AssertionError(f"Support-matrix code {c.code} is not a VXnnn code in an area.")
        if (c.disposition is Disposition.MARKER) != (c.marker is not None):
            raise AssertionError(f"{c.code}: a marker category belongs to a `marker` row, only.")
        if c.disposition is not Disposition.MECHANICAL and c.action is None:
            raise AssertionError(f"{c.code}: anything but a mechanical row needs an action.")
        if not c.detected and c.disposition is not Disposition.HAZARD:
            raise AssertionError(f"{c.code}: only a hazard can go undetected.")
    for recipe in PLUGINS.values():
        if recipe.code not in BY_CODE:
            raise AssertionError(f"Plugin {recipe.dist} cites unknown row {recipe.code}.")
    for table, label in ((BUILTIN_FIXTURES, "builtin fixture"), (INI_SETTINGS, "ini setting")):
        for name, code in table.items():
            if code not in BY_CODE:
                raise AssertionError(f"The {label} {name!r} cites unknown row {code}.")
    for name, dist in PLUGIN_MARKS.items():
        if dist not in PLUGINS:
            raise AssertionError(f"The mark {name!r} cites unknown plugin {dist!r}.")


_validate()
