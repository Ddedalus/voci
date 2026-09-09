"""The support matrix: every pytest construct a suite can contain, and what becomes of it.

One row per construct, keyed by a code (`VC214`) that stays with it from this table into the
audit's findings, into a `VOCI-TODO[category]` marker in converted source, and into the
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
        "VC001",
        "@pytest.fixture",
        MECHANICAL,
        "A fixture becomes a module-level object, and every parameter that requested it by name "
        "is annotated `Annotated[T, Depends(...)]` naming the import.",
        target="@voci.fixture()",
    ),
    _row(
        "VC002",
        "@pytest.fixture(params=...)",
        MECHANICAL,
        "The fixture keeps its cases, and `request.param` becomes the factory's own `param` "
        "argument.",
        target="@voci.fixture(params=...)",
    ),
    _row(
        "VC003",
        'fixture scope="class" or scope="package"',
        MARKER,
        "voci scopes are call, function, module and session. A class-scoped fixture becomes "
        "module-scoped and a package-scoped one becomes session-scoped, so each is shared by more "
        "tests than it was.",
        target="@voci.fixture(scope=...)",
        marker="scope",
        action="Confirm the fixture tolerates being shared more widely, or split it.",
    ),
    _row(
        "VC004",
        "conftest.py fixture",
        MECHANICAL,
        "Every fixture in a `conftest.py` moves to a `fixtures.py` beside it, and the tests that "
        "used it import it from there.",
        target="fixtures.py beside the conftest",
    ),
    _row(
        "VC005",
        "a fixture overriding one visible from further out",
        MECHANICAL,
        "The override and every fixture between it and the tests that reach it are generated as a "
        "specialized chain named for the directory or class the override rules.",
        target="a specialized fixture chain",
    ),
    _row(
        "VC006",
        "conftest override whose chain exceeds the specialization budget",
        REFUSED,
        "Specializing this override would duplicate more fixtures than the budget allows, which "
        "is the point where a generated chain stops being reviewable.",
        action="Unwind the override into an explicit seam or a parametrized fixture before "
        "converting.",
    ),
    _row(
        "VC007",
        "@pytest.mark.parametrize(..., indirect=True)",
        MECHANICAL,
        "The values move onto the fixture as `params=`: indirect parametrization is a call site "
        "choosing a fixture's case, and a voci fixture carries its own cases.",
        target="@voci.fixture(params=...)",
    ),
    _row(
        "VC008",
        "@pytest.fixture(autouse=True)",
        MECHANICAL,
        "Becomes one `voci.use(...)` declaration on the module or package the fixture covered, "
        "rather than a parameter on each test.",
        target="voci.use(...)",
    ),
    _row(
        "VC009",
        "@pytest.mark.usefixtures on a module or class",
        MECHANICAL,
        "Becomes a `voci.use(...)` declaration covering the same tests.",
        target="voci.use(...)",
    ),
    _row(
        "VC010",
        "@pytest.mark.usefixtures on some of a module's tests",
        MARKER,
        "`voci.use(...)` declares a fixture for a whole module, so the fixture reaches the "
        "module's other tests too.",
        target="voci.use(...)",
        marker="usefixtures",
        action="Confirm the other tests in the module tolerate the fixture, or move them out.",
    ),
    _row(
        "VC011",
        'request.getfixturevalue("literal")',
        MECHANICAL,
        "A literal name is a static dependency, so it becomes an ordinary `Depends()` parameter.",
        target="a Depends() parameter",
    ),
    _row(
        "VC012",
        "request.getfixturevalue(computed)",
        REFUSED,
        "The name is decided at run time, so nothing static can say which fixture is meant.",
        action="Replace the computed lookup with explicit dependencies, or with a factory "
        "fixture that takes them.",
    ),
    _row(
        "VC013",
        "request.addfinalizer(fn)",
        MECHANICAL,
        "An unconditional finalizer becomes the fixture's `yield` teardown.",
        target="yield teardown",
    ),
    _row(
        "VC014",
        "request.addfinalizer where no single yield can take its place",
        REFUSED,
        "A `yield` fixture hands its value over at one point in the body and tears down after "
        "it, so a finalizer registered under a condition, from inside another function, or from "
        "somewhere that is not a fixture has nowhere to move to.",
        action="Register the finalizer unconditionally in the fixture's own body, moving any "
        "condition inside it.",
    ),
    _row(
        "VC015",
        "request.node, request.config, request.cls, and every other attribute of request",
        MARKER,
        "`voci.test_info` carries the test's id, tags, timeout and worker. Anything else these "
        "reach — a node's own marks, ini values, the owning class — has no counterpart.",
        target="voci.test_info",
        marker="request",
        action="Rewrite the use against `test_info`, or pass what the fixture needs in.",
    ),
    _row(
        "VC016",
        "request.config.getoption on a custom pytest_addoption flag",
        UNSUPPORTED,
        "The flag exists because a conftest hook added it to pytest's command line, and voci has "
        "no plugin command-line surface for it to be added to.",
        action="Read the value from the environment or from configuration instead.",
    ),
    _row(
        "VC017",
        "request passed to another function or held past setup",
        REFUSED,
        "The uses cannot be enumerated at the call site, so no per-use translation applies.",
        action="Narrow the fixture to the values it actually reads from `request`.",
    ),
    _row(
        "VC018",
        "class Test* grouping",
        MECHANICAL,
        "The class stays as namespacing and each method collects as "
        "`path.py::TestGroup::test_name`.",
        target="class Test*",
    ),
    _row(
        "VC019",
        "setup_method, teardown_method, setup_class, setup_function",
        REFUSED,
        "voci builds a fresh instance per test and calls no setup protocol, so these never run.",
        action="Move each one's body into a fixture the class declares with `voci.use(...)`.",
    ),
    _row(
        "VC020",
        "unittest.TestCase",
        UNSUPPORTED,
        "voci collects functions and `class Test*` methods; a `TestCase` subclass brings its own "
        "lifecycle, assertions and skipping.",
        action="Rewrite the class as plain test functions.",
    ),
    _row(
        "VC021",
        "doctests",
        UNSUPPORTED,
        "voci collects test modules, not docstrings.",
        action="Keep running doctests under their own command.",
    ),
    _row(
        "VC022",
        "conftest hook (pytest_configure, pytest_collection_modifyitems, ...)",
        UNSUPPORTED,
        "Hooks are how a pytest plugin reaches into collection and reporting, and voci has no "
        "hook protocol.",
        marker=None,
        action="Decide per hook: fixtures replace setup hooks, and reporting hooks have no "
        "counterpart.",
        suite_level=True,
    ),
    _row(
        "VC023",
        "pytest_addoption",
        UNSUPPORTED,
        "voci's command line is fixed, so a suite cannot add a flag to it.",
        action="Read the setting from the environment or from `[tool.voci]`'s `env`.",
        suite_level=True,
    ),
    _row(
        "VC024",
        "pytest_generate_tests",
        MARKER,
        "The cases the hook produced are in the dump, ids included, so they convert to an "
        "explicit `@voci.parametrize` — one that lists what this extraction saw and generates "
        "nothing new.",
        target="@voci.parametrize with the generated cases",
        marker="generated-params",
        action="Confirm the frozen case list is what the suite should keep testing.",
    ),
    _row(
        "VC025",
        "pytest_plugins in a conftest",
        MARKER,
        "Fixtures the named module contributed are translated from the dump like any other, and "
        "the declaration itself goes away.",
        marker="plugin-module",
        action="Check the named module for hooks and non-fixture content, which do not travel.",
        suite_level=True,
    ),
    _row(
        "VC026",
        "a fixture requesting request for its own parametrization",
        MECHANICAL,
        "`request.param` in a `params=` fixture becomes the factory's `param` argument.",
        target="the param argument",
    ),
    _row(
        "VC027",
        "conftest override reaching an autouse fixture",
        REFUSED,
        "A specialized chain gives the subtree its own objects, and a `voci.use(...)` declares "
        "one of them for a directory — so an autouse fixture the override changes would be "
        "declared twice over the same tests, once for each definition.",
        action="Request the fixture by name where it is needed, or unwind the override.",
    ),
    _row(
        "VC028",
        "request.getfixturevalue of a name a parameter cannot carry",
        REFUSED,
        "A parameter names one object for the whole definition, so a name the suite defines in "
        "more than one directory, or one asked for where there is no signature to grow, has no "
        "parameter to become.",
        action="Request the fixture in the signature, or unwind the override that gives the "
        "name two meanings.",
    ),
    _row(
        "VC029",
        "indirect parametrization a params= fixture cannot carry",
        REFUSED,
        "A `params=` fixture has one case list for every test that reaches it, so a name given "
        "different values in different tests, one parametrized alongside a direct axis, one "
        "whose fixture already has cases of its own, and one whose values have no literal "
        "spelling all have nowhere to go.",
        action="Give the fixture the cases it always has with `params=`, or take the "
        "parametrization off the fixture and pass the value to the test.",
    ),
    _row(
        "VC030",
        "a fixture an installed plugin provides",
        UNSUPPORTED,
        "The fixture lives in a distribution, not in the suite, so there is no source to move and "
        "nothing registers it under voci.",
        action="Write the fixture into the suite, or drop the tests that need it.",
    ),
    _row(
        "VC031",
        "a generated case an explicit parametrize cannot list",
        REFUSED,
        "A frozen case list is written from the value each case was given, so a value with no "
        "literal spelling, and an axis the hook composed with one the test was written with, "
        "have nothing this can write.",
        action="Write the cases out as a `@pytest.mark.parametrize` before converting.",
    ),
    _row(
        "VC032",
        "@pytest.fixture written inside a test class",
        MECHANICAL,
        "A voci test class is pure namespacing, with no fixtures of its own, so the factory is "
        "lifted to the module level under a name carrying the class's, and the methods that "
        "requested it name it there.",
        target="a module-level fixture named for the class",
    ),
    _row(
        "VC033",
        "a fixture inside a test class reading its instance",
        REFUSED,
        "Lifting the factory out of the class takes `self` away with it, and a `self` naming "
        "anything but an attribute the class body itself binds has no module-level spelling.",
        action="Read the class attribute through the class, or move what the fixture needs off "
        "the instance.",
    ),
    _row(
        "VC034",
        "a relative import in a suite module",
        MECHANICAL,
        "voci imports test modules under synthetic names, which a leading dot resolves against "
        "nothing, so each becomes the absolute import of the same module.",
        target="an absolute import",
    ),
    _row(
        "VC035",
        "a suite that reads its own module names",
        UNSUPPORTED,
        "voci imports each test module by path, under a synthetic `voci_tests.` name that makes "
        "two files of the same name in different directories two modules. Anything keyed on "
        "`__module__` — a class registry, a plugin lookup, a snapshot path — sees that name.",
        action="Key on something the import name does not decide, or move what registers itself "
        "out of the test module.",
        detected=False,
    ),
    _row(
        "VC036",
        "a fixture requested by a positional-only parameter",
        REFUSED,
        "voci binds every argument by keyword, so a parameter written before the `/` can never "
        "be given the object it names, and moving it out from behind the `/` would change what "
        "the definition can be called with everywhere else.",
        action="Write the parameter after the `/`, or take the `/` off the signature.",
    ),
    # --- marks and parametrization -------------------------------------------------------------
    _row(
        "VC101",
        "@pytest.mark.parametrize",
        MECHANICAL,
        "Becomes `@voci.parametrize` carrying pytest's own case ids verbatim, so node ids do not "
        "move.",
        target="@voci.parametrize",
    ),
    _row(
        "VC102",
        "pytest.param(..., marks=...)",
        MECHANICAL,
        "Becomes `voci.case(*values, marks=...)`, carrying the same marks translated into their "
        "voci spelling, which reach that one case.",
        target="voci.case(..., marks=...)",
    ),
    _row(
        "VC103",
        "@pytest.mark.skipif with a string condition",
        MARKER,
        "voci takes a bool or a zero-arg callable. A string condition is evaluated by pytest at "
        "collection, and a callable is evaluated by voci at run time.",
        target="@voci.skipif",
        marker="skipif-string",
        action="Confirm the condition reads the same at run time as it did at collection.",
    ),
    _row(
        "VC104",
        "@pytest.mark.skip, @pytest.mark.skipif(bool)",
        MECHANICAL,
        "Same decorator, same reason.",
        target="@voci.skip, @voci.skipif",
    ),
    _row(
        "VC105",
        "@pytest.mark.xfail with a condition",
        MECHANICAL,
        "The condition becomes `@voci.xfail`'s own `condition=`, which decides whether the "
        "failure is expected at all.",
        target="@voci.xfail(condition=...)",
    ),
    _row(
        "VC106",
        "@pytest.mark.xfail(run=False)",
        MARKER,
        "Nothing in voci marks a test as expected-to-fail without running it, so it becomes a "
        "skip and is reported as one.",
        target="@voci.skip",
        marker="xfail-run",
        action="Confirm a skip is the outcome the suite wants reported.",
    ),
    _row(
        "VC107",
        "xfail_strict",
        MECHANICAL,
        "The ini setting is written into each generated `@voci.xfail` as `strict=`.",
        target="strict= on each xfail",
        suite_level=True,
    ),
    _row(
        "VC108",
        "@pytest.mark.filterwarnings",
        MECHANICAL,
        "Becomes `@voci.filterwarnings(...)`, which takes the same filter specs and governs the "
        "test that carries it alone, whatever else is running.",
        target="@voci.filterwarnings(...)",
    ),
    _row(
        "VC109",
        "a custom mark",
        MECHANICAL,
        "Becomes `@voci.tag(...)`, selectable with `-m`, and needs no registration.",
        target="@voci.tag(...)",
    ),
    _row(
        "VC110",
        "@pytest.mark.asyncio, @pytest.mark.anyio, event_loop fixtures",
        MECHANICAL,
        "voci runs `async def` tests itself, so the mark and the loop fixtures go away. A backend "
        "parametrization the mark carried goes with it, and with it that segment of the test's id.",
        target="deleted",
    ),
    _row(
        "VC111",
        "@pytest.mark.timeout",
        MECHANICAL,
        "Becomes `@voci.timeout(...)`.",
        target="@voci.timeout(...)",
    ),
    _row(
        "VC112",
        "a mark an installed plugin acts on",
        UNSUPPORTED,
        "The behaviour was the plugin's, and voci records marks as tags without acting on them.",
        action="Replace the mark's effect with a fixture, or drop the tests that need it.",
    ),
    _row(
        "VC113",
        "two skip, xfail or timeout marks on one test",
        REFUSED,
        "voci raises when a scalar mark is applied twice, so this is a collection error rather "
        "than a last-one-wins.",
        action="Keep one, having decided which.",
    ),
    _row(
        "VC114",
        "a case id composed from more than one axis",
        MARKER,
        "pytest builds one id per case out of every axis that varies it — stacked `parametrize` "
        "marks, or a mark over a `params=` fixture — so no single mark can carry the ids "
        "verbatim, and voci composes its own from the values.",
        target="@voci.parametrize without ids",
        marker="case-ids",
        action="Check any CI configuration, dashboard or `--last-failed` habit that names these "
        "ids, and write an explicit `ids=` where one matters.",
    ),
    _row(
        "VC115",
        "pytestmark on a module or a class",
        MECHANICAL,
        "voci reads marks from the test function, and a mark on a class is a collection error, so "
        "each mark the assignment applied becomes a decorator on every test it reached.",
        target="the same mark on each test",
    ),
    _row(
        "VC116",
        "@pytest.mark.xfail with a string condition",
        MARKER,
        "voci takes a bool or a zero-arg callable, so the string becomes a lambda over the same "
        "expression, evaluated where the module it reads is already imported.",
        target="@voci.xfail(condition=...)",
        marker="xfail-string",
        action="Confirm the condition reads the same from the converted module as it did in "
        "pytest's own namespace.",
    ),
    # --- bodies and builtin fixtures -----------------------------------------------------------
    _row(
        "VC201",
        "capsys",
        MECHANICAL,
        "Becomes the `voci.capture` fixture, whose `.out` and `.err` read the live buffers.",
        target="voci.capture",
    ),
    _row(
        "VC202",
        "capsys.readouterr() more than once in a body",
        MARKER,
        "`readouterr()` returns a snapshot and clears the buffer; `capture.out` is cumulative and "
        "never cleared, so a second read sees the first read's output too.",
        target="voci.capture",
        marker="capture",
        action="Compare against the text captured so far, or slice off what was already asserted.",
    ),
    _row(
        "VC203",
        "capfd, capsysbinary, capfdbinary",
        UNSUPPORTED,
        "voci captures by replacing `sys.stdout` and `sys.stderr`, so writes to file descriptor "
        "1 by a subprocess or a C extension are not captured, and there is no binary variant.",
        action="Redirect the subprocess to a file the test reads, or drop the assertion.",
    ),
    _row(
        "VC204",
        "caplog.records, caplog.messages",
        MECHANICAL,
        "Become the same attributes on `voci.log_records`.",
        target="voci.log_records",
    ),
    _row(
        "VC205",
        "caplog.set_level(...)",
        MARKER,
        "`LogRecords.set_level(...)` returns a context manager and does nothing until it is "
        "entered, so the statement becomes a `with` block around the rest of the test. Logger "
        "levels are process-global.",
        target="with log_records.set_level(...)",
        marker="caplog-level",
        action="Wrap the part of the test that needs the level, and mark the test `@voci.solo`.",
        serialized=True,
    ),
    _row(
        "VC206",
        "caplog.text, caplog.record_tuples, caplog.clear()",
        MECHANICAL,
        "Become the same names on `voci.log_records`, which formats `.text` from its own "
        "records on every read rather than keeping a second stream.",
        target="voci.log_records",
    ),
    _row(
        "VC207",
        "tmp_path, tmp_path_factory",
        MECHANICAL,
        "Same names, same `pathlib.Path`.",
        target="tmp_path, tmp_path_factory",
    ),
    _row(
        "VC208",
        "tmpdir, tmpdir_factory",
        MECHANICAL,
        "Same names, `voci.tmpdir`/`voci.tmpdir_factory`'s own `LegacyPath` wrapping `.join`, "
        "`.strpath`, `.write`, `.mkdir` and division the way pytest's own legacy shim does.",
        target="voci.tmpdir, voci.tmpdir_factory",
    ),
    _row(
        "VC209",
        "pytest.raises(E, match=...)",
        MECHANICAL,
        "Becomes `voci.raises`, matching with `re.search` as pytest does.",
        target="voci.raises",
    ),
    _row(
        "VC210",
        "pytest.raises(E, func, *args)",
        MECHANICAL,
        "Becomes `voci.raises`, which has the same callable form: it calls `func(*args, "
        "**kwargs)` under the hood and returns the resulting `ExceptionInfo`. Left unconverted "
        "when `match=` is among the kwargs, or a `**mapping` is unpacked into them: pytest "
        "forwards `match` to `func` there, but `voci.raises` always intercepts it.",
        target="voci.raises",
    ),
    _row(
        "VC211",
        "pytest.raises(asyncio.CancelledError)",
        UNSUPPORTED,
        "Cancellation is how voci enforces timeouts, so `voci.raises` refuses to swallow it.",
        action="Assert on the cancellation's effect instead of catching it.",
    ),
    _row(
        "VC212",
        "pytest.approx(scalar)",
        MECHANICAL,
        "Becomes `voci.approx`.",
        target="voci.approx",
    ),
    _row(
        "VC213",
        "pytest.approx over a list, tuple, or dict",
        MECHANICAL,
        "Becomes `voci.approx`, which compares a list or tuple elementwise by position and a "
        "dict elementwise by key, all under the same tolerances. Left unconverted where a list, "
        "tuple, dict, or set is nested one level inside it: `voci.approx` only walks one level, "
        "so there is no position to compare a nested container by.",
        target="voci.approx",
    ),
    _row(
        "VC214",
        "pytest.skip(), pytest.fail() as a statement",
        MECHANICAL,
        "`voci.skip` is a decorator factory, not a runtime call -- calling it in a body builds "
        "a decorator and discards it, which would turn a conditional skip into a silent pass, so "
        "these become `voci.Skipped`/`voci.Failed`, raised directly, instead.",
        target="voci.Skipped, voci.Failed",
    ),
    _row(
        "VC215",
        'pytest.importorskip("mod")',
        UNSUPPORTED,
        "There is no imperative skip to reach for at import time.",
        action="Guard the import at module level and put `@voci.skipif` on the tests.",
    ),
    _row(
        "VC216",
        "pytest.warns, recwarn, pytest.deprecated_call",
        UNSUPPORTED,
        "Recording warnings means installing a process-global filter, which cannot be scoped to "
        "one of several tests in flight.",
        action="Assert on what the warning accompanies, or catch it inside a `@voci.solo` test.",
        serialized=True,
    ),
    _row(
        "VC217",
        "unittest.mock.patch as a decorator",
        MECHANICAL,
        "The patch stays as it is and voci schedules the test alone. Injected parameters are "
        "emitted after the mock arguments the decorator fills positionally.",
        target="@mock.patch, scheduled solo",
        serialized=True,
    ),
    _row(
        "VC218",
        "unittest.mock.patch as a context manager",
        MECHANICAL,
        "A patch entered inside a body is invisible until it runs, so the test carries "
        "`@voci.solo` and nothing else runs while it holds.",
        target="@voci.solo on the test",
        serialized=True,
    ),
    _row(
        "VC219",
        "mocker (pytest-mock)",
        MARKER,
        "`mocker.patch` is `mock.patch` with teardown attached to the fixture, so each call "
        "becomes a `mock.patch` the test enters, and the test runs alone.",
        target="mock.patch plus @voci.solo",
        marker="mocker",
        action="Rewrite each `mocker.` call as the `mock` call it wraps.",
        serialized=True,
    ),
    _row(
        "VC220",
        "pytestconfig, cache, record_property, pytester and other pytest builtins",
        UNSUPPORTED,
        "These fixtures expose pytest's own configuration, cache and self-test machinery.",
        action="Drop the use, or read the value from configuration.",
    ),
    _row(
        "VC221",
        "pytest.approx over a set or a generator expression",
        UNSUPPORTED,
        "Neither has a position to compare by: a set is unordered, and a generator is spent after "
        "one read. A numpy array is left unconverted here too, since voci has no numpy "
        "dependency to compare it with.",
        action="Compare a sorted sequence instead of a set, or a list instead of a generator.",
    ),
    _row(
        "VC222",
        "caplog.handler, caplog.get_records(...)",
        UNSUPPORTED,
        "voci has no handler object behind `voci.log_records`, and no per-phase record split "
        "for `get_records` to read.",
        action="Assert on `records` or `messages` directly instead of `handler`; there is no "
        "phase-scoped equivalent for `get_records`.",
    ),
    _row(
        "VC223",
        "pytest.xfail() as a statement",
        UNSUPPORTED,
        "Unlike `pytest.skip()`/`pytest.fail()`, this has no runtime target to become: it marks "
        "the test as an expected failure and stops it right there, which `@voci.xfail(...)`'s "
        "condition -- decided once at collection, before the test has run at all -- can't reach.",
        action="Lift the condition into `@voci.xfail(condition=...)`, if it's known before the "
        "test runs; otherwise let the test fail and mark it `@voci.xfail` unconditionally.",
    ),
    # --- configuration and plugins -------------------------------------------------------------
    _row(
        "VC301",
        "testpaths",
        MECHANICAL,
        "Same meaning under `[tool.voci]`.",
        target="testpaths",
        suite_level=True,
    ),
    _row(
        "VC302",
        "python_files",
        MECHANICAL,
        "Becomes `test_file_patterns`.",
        target="test_file_patterns",
        suite_level=True,
    ),
    _row(
        "VC303",
        "norecursedirs",
        MECHANICAL,
        "Becomes `ignore`.",
        target="ignore",
        suite_level=True,
    ),
    _row(
        "VC304",
        "pytest-timeout's timeout setting",
        MECHANICAL,
        "Becomes the `timeout` key, which voci applies per test.",
        target="timeout",
        suite_level=True,
    ),
    _row(
        "VC305",
        "addopts",
        MARKER,
        "Each flag needs its own answer: some have a voci spelling, some were a plugin's, and "
        "unknown `[tool.voci]` keys are a hard error, so nothing speculative is written.",
        marker="addopts",
        action="Translate the flags you rely on into `[tool.voci]` or into the CI invocation.",
        suite_level=True,
    ),
    _row(
        "VC306",
        "markers, asyncio_mode, and other settings with nothing to configure",
        MECHANICAL,
        "voci tags need no registration and it runs async tests without being told to, so these "
        "settings have no counterpart to write.",
        target="dropped",
        suite_level=True,
    ),
    _row(
        "VC307",
        "filterwarnings",
        MECHANICAL,
        "Carried to `[tool.voci]`'s own `filterwarnings`, which takes the same filter specs.",
        target="[tool.voci] filterwarnings",
        suite_level=True,
    ),
    _row(
        "VC308",
        "log_cli, console_output_style, required_plugins and other reporting settings",
        UNSUPPORTED,
        "These configure pytest's terminal and plugin machinery.",
        action="Drop them, or reach for the closest voci flag.",
        suite_level=True,
    ),
    _row(
        "VC309",
        "an ini setting a plugin registered",
        UNSUPPORTED,
        "The setting exists because a plugin asked pytest for it, and unknown `[tool.voci]` keys "
        "are a hard error.",
        action="Drop it with the plugin, or move the value into the environment.",
        suite_level=True,
    ),
    _row(
        "VC320",
        "an installed plugin migration deletes",
        MECHANICAL,
        "Its whole job is done by the runner, so the suite loses the dependency.",
        target="deleted",
        suite_level=True,
    ),
    _row(
        "VC321",
        "an installed plugin with a translation",
        MECHANICAL,
        "What the suite uses it for has a voci spelling, reached through the rows for the "
        "fixtures and marks themselves.",
        suite_level=True,
    ),
    _row(
        "VC322",
        "an installed plugin that mutates process-global state",
        HAZARD,
        "It works, and what it changes is visible to every test running at the same time.",
        action="Schedule the tests that use it alone, or isolate them.",
        serialized=True,
        suite_level=True,
    ),
    _row(
        "VC323",
        "an installed plugin with no voci path",
        UNSUPPORTED,
        "Nothing in voci provides what it does.",
        action="Keep those tests under pytest, or drop the plugin's use.",
        suite_level=True,
    ),
    _row(
        "VC324",
        "a case parametrized onto an event loop other than asyncio",
        UNSUPPORTED,
        "voci runs every async test on asyncio, so the cases a backend fixture produces for "
        "another loop have nothing to run on and disappear with it.",
        action="Pin the backend fixture to asyncio, and keep the other loop's coverage under "
        "pytest.",
    ),
    # --- hazards -------------------------------------------------------------------------------
    _row(
        "VC401",
        "monkeypatch",
        HAZARD,
        "Every `monkeypatch` call changes state the whole process shares — an attribute, an "
        "environment variable, `sys.path`, the working directory — which a serial runner made "
        "private to the running test.",
        action="Inject the dependency instead, or mark the test `@voci.solo`; `chdir` needs "
        "`@voci.isolated`.",
        serialized=True,
    ),
    _row(
        "VC402",
        "os.environ writes",
        HAZARD,
        "The environment is process-wide, so a variable one test sets is set for every test in "
        "flight.",
        action="Set suite-wide values in `[tool.voci]`'s `env`, and mark tests that need their "
        "own value `@voci.solo`.",
        serialized=True,
    ),
    _row(
        "VC403",
        "warnings filter mutation",
        HAZARD,
        "`simplefilter` and `filterwarnings` write a process-global list, and `catch_warnings` "
        "restores it wholesale on exit — including changes another test made meanwhile.",
        action="Mark the test `@voci.solo`.",
        serialized=True,
    ),
    _row(
        "VC404",
        "logging level or handler mutation",
        HAZARD,
        "Logger objects are process-global, so a level a test raises is raised for everything "
        "logging at the same time.",
        action="Mark the test `@voci.solo`, or assert on records without changing levels.",
        serialized=True,
    ),
    _row(
        "VC405",
        "sys.modules surgery or importlib.reload",
        HAZARD,
        "Replacing or reloading a module rebinds it for every test that has already imported it.",
        action="Mark the test `@voci.isolated`.",
        serialized=True,
    ),
    _row(
        "VC406",
        "os.chdir",
        HAZARD,
        "The working directory belongs to the process, and a relative path elsewhere in the suite "
        "resolves against whatever it currently is.",
        action="Use absolute paths, or mark the test `@voci.isolated`.",
        serialized=True,
    ),
    _row(
        "VC407",
        "locale, decimal context or interpreter limits",
        HAZARD,
        "These are interpreter-wide settings with no per-task scope.",
        action="Mark the test `@voci.isolated`.",
        serialized=True,
    ),
    _row(
        "VC408",
        "a globally patched clock (freezegun, time-machine)",
        HAZARD,
        "Freezing time replaces the clock for the whole process, so every test in flight sees the "
        "frozen time.",
        action="Inject a clock, or mark the test `@voci.solo`.",
        serialized=True,
    ),
    _row(
        "VC409",
        "asyncio.run, get_event_loop, new_event_loop, run_until_complete",
        HAZARD,
        "voci runs the test on a loop that is already running, and starting a second loop from "
        "inside it fails.",
        action="Await the coroutine directly in an `async def` test.",
    ),
    _row(
        "VC410",
        "a blocking call in an async body",
        HAZARD,
        "One blocking call in an `async def` holds the loop, and every other test in flight waits "
        "for it. A sync `def` test is safe: it runs on an executor thread and holds only its own "
        "slot.",
        action="Use the async client, or make the test a plain `def`.",
    ),
    _row(
        "VC411",
        "a module-level or class-level global a test writes",
        HAZARD,
        "State that outlives a test is shared with every test that reads it, and nothing "
        "sequences them any more.",
        action="Move the state into a fixture.",
    ),
    _row(
        "VC412",
        "seeded randomness and sequence counters",
        HAZARD,
        "A seed set in a fixture, or a factory sequence, produced deterministic values because "
        "tests drew from it one at a time.",
        action="Seed per test, or assert on shape rather than on generated values.",
    ),
    _row(
        "VC413",
        "a fixed external resource",
        HAZARD,
        "A hardcoded port, path or database worked because one test used it at a time.",
        action="Allocate per test, or mark the tests that share it `exclusive=` on their fixture.",
    ),
    _row(
        "VC414",
        "test-order dependence",
        HAZARD,
        "A test that passes only because something earlier in the file ran first has nothing in "
        "its source saying so.",
        action="Run the suite shuffled under pytest before converting.",
        detected=False,
    ),
    _row(
        "VC415",
        "fixture teardown timing",
        HAZARD,
        "pytest tears a module-scoped fixture down when the module's last test finishes; voci "
        "tears it down when its last holder releases, with other modules still running.",
        action="Check anything that asserts a teardown has happened.",
        detected=False,
    ),
    _row(
        "VC416",
        "shared state behind application code",
        HAZARD,
        "Singleton caches, `functools.lru_cache` on application functions, ORM identity maps and "
        "module-level registries are shared by tests that never mention them.",
        action="Reset them in a fixture, or inject them.",
        detected=False,
    ),
)


BY_CODE: Mapping[str, Construct] = {construct.code: construct for construct in CONSTRUCTS}

# Marker categories, which are also the `VOCI-TODO[category]` spellings converted source carries.
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
# is classified `VC323` — unknown means no recipe, which is the honest answer for a plugin nobody
# has looked at.
PLUGINS: Mapping[str, PluginRecipe] = {
    recipe.dist: recipe
    for recipe in (
        PluginRecipe("pytest-asyncio", "VC320", "voci runs async tests itself."),
        PluginRecipe("anyio", "VC320", "voci runs async tests itself."),
        PluginRecipe("pytest-trio", "VC323", "voci runs tests on asyncio."),
        PluginRecipe("pytest-tornasync", "VC323", "voci runs tests on asyncio."),
        PluginRecipe(
            "pytest-xdist",
            "VC320",
            "voci runs tests concurrently in one process, so there are no workers to distribute "
            "across. Its flags live on in CI invocations.",
        ),
        PluginRecipe(
            "pytest-randomly",
            "VC320",
            "Ordering is not voci's to shuffle. A suite that ran green under it is evidence the "
            "suite does not depend on order.",
        ),
        PluginRecipe("pytest-timeout", "VC320", "`@voci.timeout(...)` and the `timeout` setting."),
        PluginRecipe("pytest-env", "VC320", "`[tool.voci]`'s `env` sets the suite's environment."),
        PluginRecipe(
            "pytest-mock",
            "VC321",
            "`mocker` becomes `mock.patch`, and the tests that use it run alone.",
        ),
        PluginRecipe(
            "pytest-httpx", "VC321", "Transport-level patching, per test, which stays as it is."
        ),
        PluginRecipe("respx", "VC321", "Transport-level patching, per test, which stays as it is."),
        PluginRecipe(
            "pytest-recording", "VC321", "Transport-level patching, per test, which stays as it is."
        ),
        PluginRecipe(
            "pytest-vcr", "VC321", "Transport-level patching, per test, which stays as it is."
        ),
        PluginRecipe(
            "pytest-freezegun", "VC322", "It patches the process clock for the running test."
        ),
        PluginRecipe(
            "pytest-freezer", "VC322", "It patches the process clock for the running test."
        ),
        PluginRecipe(
            "pytest-time-machine", "VC322", "It patches the process clock for the running test."
        ),
        PluginRecipe(
            "factory-boy",
            "VC322",
            "Factory sequences are class-level counters shared by every test drawing from them.",
        ),
        PluginRecipe(
            "pytest-django",
            "VC322",
            "Its database fixtures translate through the fixture rows, and its per-test "
            "transaction and settings overrides are process-global.",
        ),
        PluginRecipe(
            "pytest-flask",
            "VC322",
            "Its app and client fixtures translate through the fixture rows.",
        ),
        PluginRecipe(
            "pytest-postgresql",
            "VC322",
            "Its database fixtures translate through the fixture rows; the database itself is a "
            "shared resource.",
        ),
        PluginRecipe(
            "pytest-cov",
            "VC323",
            "Coverage of a concurrent single-process run is measured by running `coverage` around "
            "voci, not by a plugin.",
        ),
        PluginRecipe("pytest-subtests", "VC323", "Nothing in voci reports sub-results."),
        PluginRecipe(
            "pytest-benchmark",
            "VC323",
            "A timing measurement taken while other tests run means nothing.",
        ),
        PluginRecipe("pytest-repeat", "VC323", "Nothing in voci repeats a test."),
        PluginRecipe("pytest-rerunfailures", "VC323", "Nothing in voci retries a test."),
        PluginRecipe("pytest-flakefinder", "VC323", "Nothing in voci repeats a test."),
        PluginRecipe(
            "hypothesis",
            "VC323",
            "`@given` rewrites a test's signature, which is where "
            "voci reads injected dependencies from.",
        ),
        PluginRecipe(
            "pytest-bdd", "VC323", "Its step decorators build tests through pytest hooks."
        ),
        PluginRecipe("pytest-splinter", "VC323", "Nothing in voci provides browser fixtures."),
    )
}


# pytest's own fixtures, each keyed to the row that classifies it. A fixture a test requests that
# is neither here, nor defined in the suite, came from an installed plugin — which is `VC030`.
BUILTIN_FIXTURES: Mapping[str, str] = {
    "request": "VC026",
    "tmp_path": "VC207",
    "tmp_path_factory": "VC207",
    "tmpdir": "VC208",
    "tmpdir_factory": "VC208",
    "capsys": "VC201",
    "caplog": "VC204",
    "capfd": "VC203",
    "capsysbinary": "VC203",
    "capfdbinary": "VC203",
    "capteesys": "VC203",
    "monkeypatch": "VC401",
    "recwarn": "VC216",
    "pytestconfig": "VC220",
    "cache": "VC220",
    "record_property": "VC220",
    "record_testsuite_property": "VC220",
    "record_xml_attribute": "VC220",
    "pytester": "VC220",
    "testdir": "VC220",
    "doctest_namespace": "VC021",
}

# pytest's own ini settings, each keyed to the row that classifies it, plus the one a plugin
# registers that voci has a key for. A setting a suite writes that is not here belongs to a
# plugin, which is `VC309`.
INI_SETTINGS: Mapping[str, str] = {
    "testpaths": "VC301",
    # pytest-timeout's, and the one plugin setting `[tool.voci]` has a home for, so it is
    # classified by what becomes of it rather than by who registered it.
    "timeout": "VC304",
    "python_files": "VC302",
    "python_classes": "VC302",
    "python_functions": "VC302",
    "norecursedirs": "VC303",
    "addopts": "VC305",
    "markers": "VC306",
    "minversion": "VC306",
    "empty_parameter_set_mark": "VC306",
    "xfail_strict": "VC107",
    "strict_xfail": "VC107",
    "filterwarnings": "VC307",
    "usefixtures": "VC009",
    "consider_namespace_packages": "VC306",
    "pythonpath": "VC308",
    "required_plugins": "VC308",
    "console_output_style": "VC308",
    "junit_suite_name": "VC308",
    "junit_logging": "VC308",
    "junit_log_passing_tests": "VC308",
    "junit_duration_report": "VC308",
    "junit_family": "VC308",
    "cache_dir": "VC308",
    "verbosity_assertions": "VC308",
    "verbosity_test_cases": "VC308",
    "tmp_path_retention_count": "VC308",
    "tmp_path_retention_policy": "VC308",
    "faulthandler_timeout": "VC308",
    "doctest_optionflags": "VC021",
    "doctest_encoding": "VC021",
    "log_cli": "VC308",
    "log_cli_level": "VC308",
    "log_cli_format": "VC308",
    "log_cli_date_format": "VC308",
    "log_level": "VC308",
    "log_format": "VC308",
    "log_date_format": "VC308",
    "log_file": "VC308",
    "log_file_level": "VC308",
    "log_file_format": "VC308",
    "log_file_date_format": "VC308",
    "log_auto_indent": "VC308",
    "truncation_limit_lines": "VC308",
    "truncation_limit_chars": "VC308",
}

# Marks an installed plugin acts on, keyed to the distribution that acts on them. A mark here is
# `VC112`: voci records marks as tags and nothing acts on them.
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
    # `asyncio` and `anyio` are the runner's own answer and belong in `KNOWN_MARKS`; `trio` is not,
    # because voci runs no loop but asyncio, and a tag that silently reschedules a test onto one
    # is the whole reason this row exists.
    "trio": "pytest-trio",
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
    return PluginRecipe(dist, "VC323", "voci-migrate has no recipe for this plugin.")


def by_area() -> Iterator[tuple[Area, tuple[Construct, ...]]]:
    """Every area with its rows, in code order."""
    for area in Area:
        yield area, tuple(c for c in CONSTRUCTS if c.area is area)


def _validate_codes() -> None:
    """Every row's code is unique and a VCnnn string in a known area."""
    seen: set[str] = set()
    for c in CONSTRUCTS:
        if c.code in seen:
            raise AssertionError(f"Support-matrix code {c.code} is used twice.")
        seen.add(c.code)
        if c.code[:2] != "VC" or not c.code[2:].isdigit() or c.code[2] not in _AREA_BY_BLOCK:
            raise AssertionError(f"Support-matrix code {c.code} is not a VCnnn code in an area.")


def _validate_dispositions() -> None:
    """Every row's disposition agrees with its `marker`, `action`, and `converts` fields."""
    for c in CONSTRUCTS:
        if (c.disposition is Disposition.MARKER) != (c.marker is not None):
            raise AssertionError(f"{c.code}: a marker category belongs to a `marker` row, only.")
        if c.disposition is not Disposition.MECHANICAL and c.action is None:
            raise AssertionError(f"{c.code}: anything but a mechanical row needs an action.")
        if not c.detected and c.converts:
            raise AssertionError(f"{c.code}: a row conversion writes code for is not a blind spot.")


def _validate_cross_references() -> None:
    """Everything outside `CONSTRUCTS` that cites a row's code or a plugin cites one that exists."""
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


def _validate() -> None:
    """Guard the table's own invariants, which nothing else in the pipeline re-checks."""
    _validate_codes()
    _validate_dispositions()
    _validate_cross_references()


_validate()
