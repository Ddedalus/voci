"""A static scan of a suite's own sources for the constructs only source can witness.

A collection dump is the census of a suite's mechanical surface — its fixtures, its cases, its
marks — and it stops at the edge of a function body. This scan reads the test modules, conftests
and fixture modules themselves and reports what happens inside: the wiring a closure cannot show,
the mark shapes whose arguments have to be read, and the process-global state a body touches.

`scan` never raises for a file it cannot read: the file's name lands in `Scan.unparsed` and the
rest of the suite is still scanned, so the findings are a lower bound while that tuple is
non-empty.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import libcst as cst
import libcst.matchers as m
from libcst.metadata import (
    CodePosition,
    CodeRange,
    MetadataWrapper,
    ParentNodeProvider,
    PositionProvider,
    QualifiedNameProvider,
)

from voci_migrate.audit.findings import Finding, Site

# `request`, `capsys`, `caplog`, `monkeypatch` and `mocker` arrive as fixture parameters rather
# than as imports, so no qualified name resolves them. They are recognized by name, and only where
# an enclosing `def` pytest itself calls takes them as a parameter — a local called `request`, or a
# parameter of a function the suite calls itself, is not pytest's.
REQUEST = "request"

# What pytest collects as a test, absent a dump to read the real collected names from. `scan_source`
# falls back to this only when `known_tests` is `None`: a suite that has widened `python_functions`
# away from this prefix is reported under VC302, and reads here as one whose helpers are not tests,
# which is the conservative direction -- `scan`, called with a dump in hand, does not take it.
_TEST_PREFIX = "test"

# What each `request` attribute reaches, for the message. `config` is here for the reads other than
# `getoption`, which has its own row.
_REQUEST_ATTRS: Mapping[str, str] = {
    "node": "the test's collection node",
    "config": "pytest's own configuration",
    "cls": "the class the test belongs to",
    "instance": "the test instance",
    "fixturenames": "the names in this test's fixture closure",
    "session": "the collection session",
    "scope": "the scope the fixture was requested at",
    "keywords": "the test's keywords",
    "module": "the test's module",
    "function": "the test function object",
}

# The `request` attributes a conversion has a translation for: the case a `params=` fixture reads,
# the static dependency behind a literal `getfixturevalue`, and the teardown a `yield` says. Every
# other attribute survives the rewrite, which is what makes it a finding of its own.
_REQUEST_ELIMINABLE = frozenset({"param", "getfixturevalue", "addfinalizer"})

_CAPLOG_ATTRS: Mapping[str, str] = {
    "handler": "reaches the handler behind the records",
    "get_records": "reads one test phase's records",
}
_CAPLOG_METHODS = frozenset({"get_records"})

_MONKEYPATCH_EFFECTS: Mapping[str, str] = {
    "setenv": "writes an environment variable every test in flight can see",
    "delenv": "removes an environment variable every test in flight can see",
    "setattr": "rebinds an attribute every test in flight shares",
    "delattr": "deletes an attribute every test in flight shares",
    "setitem": "writes a mapping every test in flight shares",
    "delitem": "deletes from a mapping every test in flight shares",
    "syspath_prepend": "writes `sys.path` for the whole process",
    "chdir": "changes the working directory, so this test needs isolation",
}
_MONKEYPATCH_OTHER = "changes state the whole process shares"

# pytest's xunit protocol, split by where a name means anything: a `setup_method` written inside
# another function, or at module level, is an ordinary function with a familiar name.
_CLASS_SETUP = frozenset({"setup_method", "teardown_method", "setup_class", "teardown_class"})
_MODULE_SETUP = frozenset(
    {"setup_function", "teardown_function", "setup_module", "teardown_module"}
)

_TEST_CASE = frozenset({"unittest.TestCase", "unittest.case.TestCase"})
_CANCELLED = frozenset({"asyncio.CancelledError", "asyncio.exceptions.CancelledError"})

# A call recognized by the qualified name of what it calls, with the tail of its message. The
# message reads "`<name>(...)` <tail>".
_CALLS: Mapping[str, tuple[str, str]] = {
    "pytest.importorskip": ("VC215", "skips the rest of the module if the import fails."),
    "pytest.warns": ("VC216", "records warnings through a process-wide filter."),
    "pytest.deprecated_call": ("VC216", "records warnings through a process-wide filter."),
    "pytest.mark.filterwarnings": ("VC108", "sets a warning filter for one test."),
    "os.putenv": ("VC402", "writes a variable into the process environment."),
    "os.unsetenv": ("VC402", "removes a variable from the process environment."),
    "warnings.simplefilter": ("VC403", "replaces the process-wide warning filters."),
    "warnings.filterwarnings": ("VC403", "adds to the process-wide warning filters."),
    "warnings.resetwarnings": ("VC403", "clears the process-wide warning filters."),
    "warnings.catch_warnings": ("VC403", "saves and restores the process-wide warning filters."),
    "logging.basicConfig": ("VC404", "configures the root logger for the whole process."),
    "logging.disable": ("VC404", "silences a level for everything that logs."),
    "importlib.reload": ("VC405", "re-executes a module every importer already holds."),
    "os.chdir": ("VC406", "changes the working directory of the whole process."),
    "contextlib.chdir": ("VC406", "changes the working directory of the whole process."),
    "locale.setlocale": ("VC407", "sets the locale for the whole interpreter."),
    "decimal.setcontext": ("VC407", "sets the decimal context for the whole thread."),
    "decimal.localcontext": ("VC407", "sets the decimal context for the whole thread."),
    "sys.setrecursionlimit": ("VC407", "sets the interpreter's recursion limit."),
    "sys.setswitchinterval": ("VC407", "sets the interpreter's thread switch interval."),
    "freezegun.freeze_time": ("VC408", "replaces the clock the whole process reads."),
    "freeze_time": ("VC408", "replaces the clock the whole process reads."),
    "time_machine.travel": ("VC408", "replaces the clock the whole process reads."),
    "asyncio.run": ("VC409", "starts an event loop of its own."),
    "asyncio.get_event_loop": ("VC409", "reaches for an event loop of its own."),
    "asyncio.new_event_loop": ("VC409", "creates an event loop of its own."),
    "asyncio.set_event_loop": ("VC409", "installs an event loop of its own."),
    "random.seed": ("VC412", "seeds the random generator the whole process draws from."),
    "numpy.random.seed": ("VC412", "seeds numpy's process-wide random generator."),
}

# A call recognized by the method being called, whatever it is called on, with its message tail.
# The message reads "`<what was called>(...)` <tail>".
_METHOD_CALLS: Mapping[str, tuple[str, str]] = {
    "setLevel": ("VC404", "changes the level of a logger everything else logs through."),
    "addHandler": ("VC404", "attaches a handler to a logger the whole process shares."),
    "removeHandler": ("VC404", "removes a handler from a logger the whole process shares."),
    "run_until_complete": ("VC409", "drives an event loop of its own."),
    "run_forever": ("VC409", "drives an event loop of its own."),
    "reset_sequence": ("VC412", "rewinds a counter every test drawing from it shares."),
}

# The calls that hold the loop when they run in an `async def`. A sync `def` runs on an executor
# thread, where the same call blocks nothing but itself, so this list is only consulted in async
# bodies.
_BLOCKING = frozenset(
    {
        "time.sleep",
        "urllib.request.urlopen",
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "os.system",
        "socket.socket",
        "socket.create_connection",
        "sqlite3.connect",
        "psycopg2.connect",
    }
)

_ENVIRON_WRITES = frozenset({"update", "setdefault", "pop", "clear"})

# What `pytest.approx`'s first argument is, where `voci.approx` has no position to compare it
# by — shared with `voci_migrate.convert.rules.bodies._Approx`, which refuses the same shapes.
APPROX_KINDS: Mapping[type[cst.CSTNode], str] = {
    cst.Set: "a set",
    cst.SetComp: "a set comprehension",
    cst.GeneratorExp: "a generator expression",
}

# The literal container types one level of nesting refuses, inside a list, tuple, or dict literal
# passed to `pytest.approx` — shared with `voci_migrate.convert.rules.bodies._Approx`, which
# refuses the same shape.
_APPROX_NESTED_LITERALS = (cst.List, cst.Tuple, cst.Dict, cst.Set)

_FAKERS = frozenset({"Faker", "faker"})
_ADDRESS = re.compile(r"(?:localhost|127\.0\.0\.1):\d+")
_FIXED_PATHS = ("/tmp/", "/var/tmp/")

_EMPTY_MODULE = cst.Module(body=())

# The parser gives every node a position, so this stands in for one only where a node came from
# somewhere else — and it is what keeps `get_metadata`'s result a `CodeRange`.
_NO_POSITION = CodeRange(CodePosition(0, 0), CodePosition(0, 0))


@dataclass(frozen=True, slots=True)
class Scan:
    """What one pass over a suite's sources found.

    `unparsed` holds the rootdir-relative names of the files whose source could not be read, and
    `files` counts the ones that could.
    """

    findings: tuple[Finding, ...]
    unparsed: tuple[str, ...]
    files: int


def scan(
    paths: Iterable[Path], *, root: Path, known_tests: Mapping[str, frozenset[str]] | None = None
) -> Scan:
    """Every finding in `paths`, sited relative to `root`.

    A path that is not a file is skipped without comment; one whose bytes or syntax cannot be read
    is named in `Scan.unparsed` and the scan goes on. `known_tests` is the dump's own qualnames
    -- `Class.method` or a bare function name -- collected as tests in each path, keyed the same
    way; a path this does not mention reads as holding none, which is right for a `conftest.py` and
    wrong only where a caller forgot to name a file it should have. `None` leaves every path to
    `scan_source`'s own name-prefix guess, for a caller with no dump to read.
    """
    findings: list[Finding] = []
    unparsed: list[str] = []
    files = 0
    seen: set[Path] = set()

    for path in paths:
        key = _key(path)
        if key in seen:
            continue
        seen.add(key)
        if not path.is_file():
            continue
        relative = _relative(path, root)
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            unparsed.append(relative)
            continue
        tests = None if known_tests is None else known_tests.get(relative, frozenset())
        try:
            findings.extend(scan_source(source, path=relative, known_tests=tests))
        except cst.ParserSyntaxError:
            unparsed.append(relative)
            continue
        files += 1

    return Scan(
        findings=tuple(sorted(findings, key=lambda finding: finding.sort_key)),
        unparsed=tuple(sorted(set(unparsed))),
        files=files,
    )


def scan_source(
    source: str, *, path: str, known_tests: frozenset[str] | None = None
) -> tuple[Finding, ...]:
    """Every finding in `source`, sited at `path` verbatim.

    `known_tests` names the qualnames -- `Class.method` or a bare function name -- that a dump
    resolved as this file's own tests, which decides what `request` forwarded into a plain-looking
    `def` still counts as pytest's; `None` (the default, for a caller with no dump) falls back to
    `_TEST_PREFIX`. Raises `libcst.ParserSyntaxError` for source it cannot parse.
    """
    wrapper = MetadataWrapper(cst.parse_module(source), unsafe_skip_copy=True)
    module_names = _module_level_names(wrapper.module)
    forwarded: frozenset[str] = frozenset()
    scanner = _Scanner(path, module_names, known_tests=known_tests, forwarded=forwarded)
    wrapper.visit(scanner)
    # Re-scan for as long as a pass finds a function newly forwarded `request` -- a forwarding call
    # site can be written above or below the `def` it hands `request` to, and a chain of helpers
    # (a test forwards into one, which forwards into another) only surfaces its outermost link on
    # the first pass, its next on the second, and so on. Bounded by the module's own finite set of
    # callable names, so this always settles.
    while scanner.forwarded_targets - forwarded:
        forwarded = scanner.forwarded_targets
        scanner = _Scanner(path, module_names, known_tests=known_tests, forwarded=forwarded)
        wrapper.visit(scanner)
    return scanner.findings


@dataclass(slots=True)
class _Frame:
    """One enclosing `def` or `class`, with what the scan needs while it is inside.

    `blocks` are the conditional constructs entered since this frame opened — the ones whose body
    may not run — which is what tells a conditionally registered finalizer from an unconditional
    one. A `with` body is not among them: entering can raise, but then nothing after it runs
    either. A `try` body is, because its own handler can swallow the failure that stopped it
    halfway and let the rest of the function run without it. `params` and `locals` are empty for a
    class, whose body binds nothing a nested function sees.
    """

    name: str
    params: frozenset[str] = frozenset()
    locals: frozenset[str] = frozenset()
    is_function: bool = False
    is_async: bool = False
    #: Whether pytest fills this function's parameters in. Only a fixture factory and a test have
    #: their arguments injected; every other `def` is called by code that passes its own, so a
    #: parameter named `request` there is that code's object rather than pytest's.
    injects: bool = False
    blocks: list[str] = field(default_factory=list)
    readouterr: list[cst.Call] = field(default_factory=list)
    #: For a class frame, the names its own body binds — what a `self` inside it can still mean
    #: once a fixture written there is lifted to the module level.
    attributes: frozenset[str] = frozenset()


class _Scanner(cst.CSTVisitor):
    """One pass over one module, collecting findings as it goes.

    At most one finding per code, line and enclosing function survives, so a construct written
    twice on a line is reported once, keeping the first message.
    """

    METADATA_DEPENDENCIES = (PositionProvider, QualifiedNameProvider, ParentNodeProvider)

    def __init__(
        self,
        path: str,
        module_names: frozenset[str],
        *,
        known_tests: frozenset[str] | None = None,
        forwarded: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__()
        self._path = path
        self._module_names = module_names
        # `None` here means "no dump to read", which is what falls `visit_FunctionDef` back to
        # `_TEST_PREFIX`; an empty set, by contrast, is a dump's own answer that this file has no
        # tests, which is just as good an answer as a populated one.
        self._known_tests = known_tests
        # The functions a first pass over this same module found `request` forwarded into by name;
        # empty on that first pass, since nothing has been discovered yet.
        self._forwarded = forwarded
        self._stack: list[_Frame] = [_Frame(name="")]
        # Names bound to a `mock.patch(...)` object, so a later `.start()` on one is recognized.
        # Kept per module rather than per scope, since the pattern is always write-then-start.
        self._patchers: set[str] = set()
        self._findings: dict[tuple[str, int, str], Finding] = {}
        # Functions in this file seen handed this frame's own pytest `request` by name (qualified,
        # so two functions sharing a plain name in different scopes are not conflated), for a later
        # pass to treat as injecting it too.
        self._forwarded_targets: set[str] = set()

    @property
    def findings(self) -> tuple[Finding, ...]:
        return tuple(sorted(self._findings.values(), key=lambda f: (f.site.line or 0, f.code)))

    @property
    def forwarded_targets(self) -> frozenset[str]:
        return frozenset(self._forwarded_targets)

    # --- scopes ------------------------------------------------------------------------------

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        name = node.name.value
        at_module_level = len(self._stack) == 1
        in_class = not at_module_level and not self._stack[-1].is_function
        params = _param_names(node.params)
        # `QualifiedNameProvider` resolves a nested class's own dotted chain
        # (`TestOuter.TestInner.test_a`) and a nested function's (`test_b.<locals>.helper`), the
        # same two shapes `known_tests` and `_forwarded` are keyed by -- the first from the dump's
        # own `item.cls.__qualname__`, the second from resolving a call site the same way.
        qualified = self._names(node)
        if self._known_tests is None:
            is_test = name.startswith(_TEST_PREFIX) and (at_module_level or in_class)
        else:
            is_test = (at_module_level or in_class) and bool(qualified & self._known_tests)
        self._stack.append(
            _Frame(
                name=name,
                params=params,
                locals=_assigned_names(node),
                is_function=True,
                is_async=node.asynchronous is not None,
                injects=_is_fixture_def(node)
                or is_test
                or (REQUEST in params and bool(qualified & self._forwarded)),
            )
        )
        if in_class and _is_fixture_def(node):
            stranded = self._stranded_self(node, self._stack[-2].attributes)
            if stranded is not None:
                self._report(
                    "VC033",
                    stranded,
                    f"`{self._function}` reads `self`, which the module level has no name for.",
                )
        if (name in _CLASS_SETUP and in_class) or (name in _MODULE_SETUP and at_module_level):
            self._report(
                "VC019",
                node.name,
                f"`{self._function}` is part of pytest's setup and teardown protocol.",
            )
        elif at_module_level and name == "pytest_addoption":
            self._report("VC023", node.name, "`pytest_addoption` adds a flag to pytest's own CLI.")
        elif at_module_level and name == "pytest_generate_tests":
            self._report(
                "VC024",
                node.name,
                "`pytest_generate_tests` builds cases while pytest collects this module.",
            )
        elif at_module_level and name.startswith("pytest_"):
            self._report("VC022", node.name, f"`{name}` is a pytest hook.")

    def leave_FunctionDef(self, original_node: cst.FunctionDef) -> None:
        frame = self._stack[-1]
        if len(frame.readouterr) > 1:
            self._report(
                "VC202",
                frame.readouterr[1],
                f"`capsys.readouterr()` is called {len(frame.readouterr)} times in this test.",
            )
        self._stack.pop()

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self._stack.append(_Frame(name=node.name.value, attributes=_class_attributes(node)))
        for base in node.bases:
            if self._names(base.value) & _TEST_CASE:
                self._report(
                    "VC020",
                    node.name,
                    f"`class {node.name.value}` subclasses `unittest.TestCase`.",
                )

    def leave_ClassDef(self, original_node: cst.ClassDef) -> None:
        self._stack.pop()

    def visit_If(self, node: cst.If) -> None:
        self._enter_block("if")

    def leave_If(self, original_node: cst.If) -> None:
        self._leave_block()

    def visit_For(self, node: cst.For) -> None:
        self._enter_block("for")

    def leave_For(self, original_node: cst.For) -> None:
        self._leave_block()

    def visit_Try(self, node: cst.Try) -> None:
        self._enter_block("try")

    def leave_Try(self, original_node: cst.Try) -> None:
        self._leave_block()

    def visit_TryStar(self, node: cst.TryStar) -> None:
        self._enter_block("try")

    def leave_TryStar(self, original_node: cst.TryStar) -> None:
        self._leave_block()

    def visit_While(self, node: cst.While) -> None:
        self._enter_block("while")

    def leave_While(self, original_node: cst.While) -> None:
        self._leave_block()

    def visit_Else(self, node: cst.Else) -> None:
        self._enter_block("else")

    def leave_Else(self, original_node: cst.Else) -> None:
        self._leave_block()

    def visit_ExceptHandler(self, node: cst.ExceptHandler) -> None:
        self._enter_block("except")

    def leave_ExceptHandler(self, original_node: cst.ExceptHandler) -> None:
        self._leave_block()

    def visit_ExceptStarHandler(self, node: cst.ExceptStarHandler) -> None:
        self._enter_block("except")

    def leave_ExceptStarHandler(self, original_node: cst.ExceptStarHandler) -> None:
        self._leave_block()

    def visit_MatchCase(self, node: cst.MatchCase) -> None:
        self._enter_block("case")

    def leave_MatchCase(self, original_node: cst.MatchCase) -> None:
        self._leave_block()

    def visit_With(self, node: cst.With) -> None:
        for item in node.items:
            entered = item.item
            if isinstance(entered, cst.Call) and _is_mock_patch(self._names(entered)):
                self._report(
                    "VC218",
                    entered,
                    f"`{_render(entered)}` is entered inside the body, where nothing outside the "
                    "test can see it.",
                )

    def visit_Import(self, node: cst.Import) -> bool:
        return False

    def visit_ImportFrom(self, node: cst.ImportFrom) -> bool:
        return False

    # --- calls -------------------------------------------------------------------------------

    def visit_Call(self, node: cst.Call) -> None:
        names = self._names(node)
        self._tabled_call(node, names)
        self._pytest_call(node, names)
        self._fixture_call(node)
        self._hazard_call(node, names)
        self._forwarding_call(node)

    def _forwarding_call(self, node: cst.Call) -> None:
        """Record `node` where it hands this frame's own pytest `request` to a function defined
        somewhere in this same file, by the same name -- `helper(request)` or
        `helper(request=request)` -- so a later pass can tell that function's identically named
        parameter is pytest's `request` too. A call into another file is not seen this way: this
        scan is one file at a time, so a helper imported from a sibling module keeps its `request`
        parameter unrecognized.

        Only a bare call to a name, not a method or an attribute, since there is no signature to
        match a keyword against and no body to re-scan for anything else. The target is recorded by
        its resolved qualified name -- `helper`, or `test_b.<locals>.helper` for one nested inside
        another function -- so two functions sharing a plain name in different scopes are not
        conflated. A positional match is taken on faith until a later pass finds the target
        actually declares a `request` parameter of its own; a keyword spelled anything else lands on
        a parameter this scan cannot follow, so it is left as the existing `VC017` finding at this
        call site says.
        """
        func = node.func
        if not isinstance(func, cst.Name) or not self._takes(REQUEST):
            return
        forwards = any(
            isinstance(arg.value, cst.Name)
            and arg.value.value == REQUEST
            and (arg.keyword is None or arg.keyword.value == REQUEST)
            for arg in node.args
        )
        if forwards:
            self._forwarded_targets.update(self._names(node))

    def _tabled_call(self, node: cst.Call, names: frozenset[str]) -> None:
        for name in sorted(names):
            tabled = _CALLS.get(name)
            if tabled is not None:
                code, tail = tabled
                self._report(code, node, f"`{name}(...)` {tail}")
                return
        func = node.func
        if isinstance(func, cst.Attribute):
            tabled = _METHOD_CALLS.get(func.attr.value)
            if tabled is not None:
                code, tail = tabled
                self._report(code, node, f"`{_render(func)}(...)` {tail}")

    def _pytest_call(self, node: cst.Call, names: frozenset[str]) -> None:
        positional = _positional(node)
        if "pytest.raises" in names:
            self._raises_call(node, positional)
        elif "pytest.approx" in names and positional:
            self._approx_call(node, positional)
        elif "pytest.param" in names:
            self._param_call(node)
        elif "pytest.mark.skipif" in names:
            self._skipif_call(node, positional)
        elif "pytest.mark.xfail" in names:
            self._xfail_call(node, positional)

    def _raises_call(self, node: cst.Call, positional: list[cst.Arg]) -> None:
        entered = isinstance(self.get_metadata(ParentNodeProvider, node, None), cst.WithItem)
        if not entered and len(positional) < 2:
            self._report(
                "VC210",
                node,
                "`pytest.raises` is neither entered by a `with` nor called with a second "
                "positional argument for it to call, so its result is being stashed for later.",
            )
        elif not entered and len(positional) >= 2 and _keyword(node, "match") is not None:
            self._report(
                "VC210",
                node,
                "`pytest.raises` is called with `match=` and a callable to call: pytest forwards "
                "`match` to the callable there, but `voci.raises` always intercepts it to match "
                "the exception.",
            )
        if positional and self._names(positional[0].value) & _CANCELLED:
            self._report(
                "VC211", node, "`pytest.raises` is asked to catch `asyncio.CancelledError`."
            )

    def _approx_call(self, node: cst.Call, positional: list[cst.Arg]) -> None:
        found = self._approx_kind(positional[0].value)
        if found is not None:
            code, message = found
            self._report(code, node, message)

    def _param_call(self, node: cst.Call) -> None:
        marks = _keyword(node, "marks")
        if marks is not None:
            self._report(
                "VC102", node, f"`pytest.param` puts `{_render(marks.value)}` on one case."
            )

    def _skipif_call(self, node: cst.Call, positional: list[cst.Arg]) -> None:
        condition = _condition(node, positional)
        if isinstance(condition, cst.SimpleString | cst.ConcatenatedString):
            self._report(
                "VC103",
                node,
                f"`@pytest.mark.skipif` is given the string condition `{_render(condition)}`.",
            )

    def _xfail_call(self, node: cst.Call, positional: list[cst.Arg]) -> None:
        condition = _condition(node, positional)
        if condition is not None:
            self._report(
                "VC105",
                node,
                f"`@pytest.mark.xfail` expects a failure only when `{_render(condition)}`.",
            )
        run = _keyword(node, "run")
        if run is not None and _is_false(run.value):
            self._report(
                "VC106",
                node,
                "`@pytest.mark.xfail(run=False)` expects a failure without running the test.",
            )

    def _approx_kind(self, argument: cst.BaseExpression) -> tuple[str, str] | None:
        """The code and message for why `pytest.approx(argument)` will not convert to
        `voci.approx`, or `None` where it will.

        VC221 for a set, a set comprehension, a generator expression, or a numpy array — there is
        no position to compare any of those by. VC213 for a list, tuple, or dict literal with a
        list, tuple, dict, or set literal nested one level inside it, the same shape
        `voci_migrate.convert.rules.bodies._Approx` refuses to convert.
        """
        kind = APPROX_KINDS.get(type(argument))
        if kind is not None:
            return "VC221", f"`pytest.approx` is given {kind}."
        if isinstance(argument, cst.Call):
            called = _dotted(argument.func) or ""
            if called.split(".")[0] in ("np", "numpy") or any(
                name.startswith("numpy.") for name in self._names(argument)
            ):
                return "VC221", "`pytest.approx` is given a `numpy` array."
        nested = approx_nested(argument)
        if nested is not None:
            return (
                "VC213",
                f"`pytest.approx` is given `{_render(argument)}`, with `{_render(nested)}` "
                "nested inside it.",
            )
        return None

    def _fixture_call(self, node: cst.Call) -> None:
        func = node.func
        if not isinstance(func, cst.Attribute):
            return
        attr = func.attr.value
        if self._is_fixture(func.value, REQUEST):
            self._request_call(node, attr)
        elif self._is_fixture(func.value, "mocker"):
            self._report(
                "VC219", node, f"`mocker.{attr}(...)` patches through pytest-mock's fixture."
            )
        elif self._is_fixture(func.value, "caplog") and attr == "set_level":
            self._report(
                "VC205", node, f"`caplog.set_level({_arguments(node)})` sets a logger's level."
            )
        elif self._is_fixture(func.value, "capsys") and attr == "readouterr":
            self._stack[-1].readouterr.append(node)
        elif (
            attr == "getoption"
            and isinstance(func.value, cst.Attribute)
            and func.value.attr.value == "config"
            and self._is_fixture(func.value.value, REQUEST)
        ):
            self._report(
                "VC016",
                node,
                f"`request.config.getoption({_arguments(node)})` reads a pytest command-line flag.",
            )

    def _request_call(self, node: cst.Call, attr: str) -> None:
        arguments = _arguments(node)
        if attr == "getfixturevalue":
            positional = _positional(node)
            requested = positional[0].value if positional else None
            named = requested.evaluated_value if isinstance(requested, cst.SimpleString) else None
            if isinstance(named, str):
                self._report(
                    "VC011",
                    node,
                    f"`request.getfixturevalue({arguments})` requests one named fixture.",
                    {"requested": named},
                )
            else:
                self._report(
                    "VC012",
                    node,
                    f"`request.getfixturevalue({arguments})` names its fixture with an expression.",
                )
        elif attr == "addfinalizer":
            blocks = self._stack[-1].blocks
            if blocks:
                self._report(
                    "VC014",
                    node,
                    f"`request.addfinalizer({arguments})` is registered inside a `{blocks[-1]}` "
                    "block.",
                )
            else:
                self._report(
                    "VC013",
                    node,
                    f"`request.addfinalizer({arguments})` registers a teardown callback.",
                )

    def _hazard_call(self, node: cst.Call, names: frozenset[str]) -> None:
        if self._in_async:
            blocking = _blocking_name(names)
            if blocking is not None:
                self._report(
                    "VC410",
                    node,
                    f"`{blocking}(...)` blocks the event loop this `async def` runs on.",
                )
        func = node.func
        if not isinstance(func, cst.Attribute):
            return
        attr = func.attr.value
        called_on = self._names(func.value)
        if "os.environ" in called_on and attr in _ENVIRON_WRITES:
            self._report("VC402", node, f"`os.environ.{attr}(...)` writes the process environment.")
        elif "sys.modules" in called_on and attr == "pop":
            self._report(
                "VC405", node, "`sys.modules.pop(...)` unimports a module for every importer."
            )
        elif any(name.startswith("logging.root.") for name in names):
            self._report("VC404", node, f"`{_render(func)}(...)` mutates the root logger.")
        elif attr == "start" and (
            _is_mock_patch(self._names(func.value))
            or (isinstance(func.value, cst.Name) and func.value.value in self._patchers)
        ):
            self._report(
                "VC218",
                node,
                f"`{_render(func.value)}` is started inside the body, where nothing outside the "
                "test can see it.",
            )
        elif attr.startswith("seed") and _faker_target(func.value):
            self._report("VC412", node, f"`{_render(func)}(...)` seeds a shared Faker instance.")

    # --- statements and expressions ----------------------------------------------------------

    def visit_Decorator(self, node: cst.Decorator) -> None:
        decorator = node.decorator
        call = decorator if isinstance(decorator, cst.Call) else None
        names = self._names(call.func if call is not None else decorator)
        if _is_mock_patch(names):
            positional = _positional(call) if call is not None else []
            patched = _render(positional[0].value) if positional else "a name"
            self._report(
                "VC217", node, f"`{sorted(names)[0]}` patches `{patched}` for the whole test."
            )

    def visit_Expr(self, node: cst.Expr) -> None:
        call = node.value
        if not isinstance(call, cst.Call):
            return
        names = self._names(call)
        if "pytest.xfail" in names:
            self._report(
                "VC223", call, f"`pytest.xfail({_arguments(call)})` is called as a statement."
            )
            return
        for name in ("pytest.skip", "pytest.fail"):
            if name in names and imperative_reason_args(call, name) is None:
                self._report(
                    "VC214", call, f"`{name}({_arguments(call)})` is called as a statement."
                )
                return

    def visit_Assign(self, node: cst.Assign) -> None:
        targets = [target.target for target in node.targets]
        if len(self._stack) == 1 and any(_bound_name(t) == "pytest_plugins" for t in targets):
            self._report(
                "VC025", node, f"`pytest_plugins` names `{_render(node.value)}` as a plugin module."
            )
        for target in targets:
            self._written(target)
        if _is_mock_patch(self._names(node.value)):
            self._patchers.update(name for t in targets if (name := _bound_name(t)))

    def visit_AugAssign(self, node: cst.AugAssign) -> None:
        self._written(node.target)

    def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
        self._written(node.target)

    def visit_Del(self, node: cst.Del) -> None:
        target = node.target
        if not isinstance(target, cst.Subscript):
            return
        container = self._names(target.value)
        if "os.environ" in container:
            self._report(
                "VC402",
                node,
                f"`del {_render(target)}` removes a variable from the process environment.",
            )
        elif "sys.modules" in container:
            self._report(
                "VC405", node, f"`del {_render(target)}` unimports a module for every importer."
            )

    def visit_Global(self, node: cst.Global) -> None:
        if self._in_function:
            names = ", ".join(item.name.value for item in node.names)
            self._report("VC411", node, f"`global {names}` rebinds a name the whole module shares.")

    def visit_Attribute(self, node: cst.Attribute) -> None:
        value = node.value
        attr = node.attr.value
        if self._is_fixture(value, REQUEST):
            if attr in _REQUEST_ELIMINABLE:
                return
            if attr == "config" and self._parent_attribute(node) == "getoption":
                return
            reaches = _REQUEST_ATTRS.get(attr, "the request object pytest builds per fixture")
            self._report("VC015", node, f"`request.{attr}` reads {reaches}.")
        elif self._is_fixture(value, "caplog"):
            reads = _CAPLOG_ATTRS.get(attr)
            if reads is not None:
                called = "()" if attr in _CAPLOG_METHODS else ""
                self._report("VC222", node, f"`caplog.{attr}{called}` {reads}.")
        elif self._is_fixture(value, "monkeypatch"):
            effect = _MONKEYPATCH_EFFECTS.get(attr, _MONKEYPATCH_OTHER)
            self._report("VC401", node, f"`monkeypatch.{attr}` {effect}.")

    def visit_Name(self, node: cst.Name) -> None:
        if node.value != REQUEST or not self._takes(REQUEST):
            return
        parent = self.get_metadata(ParentNodeProvider, node, None)
        if _is_binding(parent, node):
            return
        # `request.<attr>` is the shape every other row is written against; anything else hands the
        # object itself somewhere this scan cannot follow.
        if isinstance(parent, cst.Attribute):
            return
        self._report(
            "VC017", node, "`request` itself is used as a value, not read attribute by attribute."
        )

    def visit_Arg(self, node: cst.Arg) -> None:
        keyword = node.keyword
        if keyword is not None and keyword.value.lower() == "port" and _is_integer(node.value):
            self._report(
                "VC413", node, f"`{keyword.value}={_render(node.value)}` is a fixed port number."
            )

    def visit_SimpleString(self, node: cst.SimpleString) -> None:
        # A string that is a statement of its own is a docstring, where a path or an address is
        # being described rather than used.
        if isinstance(self.get_metadata(ParentNodeProvider, node, None), cst.Expr):
            return
        text = node.evaluated_value
        if not isinstance(text, str):
            return
        if _ADDRESS.search(text):
            self._report("VC413", node, f"`{text}` is a fixed host and port.")
        elif text.startswith(_FIXED_PATHS):
            self._report("VC413", node, f"`{text}` is a fixed filesystem path.")

    # --- scope helpers -----------------------------------------------------------------------

    def _written(self, target: cst.BaseExpression) -> None:
        """Record what writing to `target` costs, for the rows about shared state."""
        if isinstance(target, cst.Subscript):
            container = self._names(target.value)
            if "os.environ" in container:
                self._report(
                    "VC402",
                    target,
                    f"`{_render(target)}` is assigned, writing the environment "
                    "every test in flight reads.",
                )
            elif "sys.modules" in container:
                self._report(
                    "VC405",
                    target,
                    f"`{_render(target)}` replaces a module for every importer.",
                )
        if isinstance(target, cst.Attribute) and any(
            name.startswith("logging.root") for name in self._names(target)
        ):
            self._report("VC404", target, f"`{_render(target)}` mutates the root logger.")
        root = _root_name(target)
        if (
            self._in_function
            and isinstance(target, cst.Attribute | cst.Subscript)
            and root is not None
            and root in self._module_names
            and not self._binds(root)
        ):
            self._report(
                "VC411",
                target,
                f"`{_render(target)}` writes `{root}`, which lives at module level.",
            )

    def _stranded_self(
        self, node: cst.FunctionDef, attributes: frozenset[str]
    ) -> cst.CSTNode | None:
        """The first `self` in this factory that lifting it out of the class would strand.

        Reading an attribute the class body itself binds survives the move — the class is still
        there to read it through. Everything else does not: `self` passed somewhere, an attribute
        only an instance has, or an assignment onto the instance the test is about to be given.

        A `self` inside a nested `def` that declares one of its own is that function's, not this
        factory's, and the move leaves it exactly where it was.
        """
        for name in m.findall(node, m.Name("self")):
            if self._rebound(node, name):
                continue
            parent = self.get_metadata(ParentNodeProvider, name, None)
            if isinstance(parent, cst.Param):
                continue
            if not isinstance(parent, cst.Attribute) or parent.value is not name:
                return name
            if parent.attr.value not in attributes:
                return name
            above = self.get_metadata(ParentNodeProvider, parent, None)
            if isinstance(above, cst.AssignTarget | cst.AugAssign | cst.AnnAssign):
                return name
        return None

    def _rebound(self, factory: cst.FunctionDef, name: cst.CSTNode) -> bool:
        """Whether a `def` between `name` and `factory` declares a `self` of its own."""
        node: cst.CSTNode | None = self.get_metadata(ParentNodeProvider, name, None)
        while node is not None and node is not factory:
            if isinstance(node, cst.FunctionDef) and _declares_receiver(node):
                return True
            node = self.get_metadata(ParentNodeProvider, node, None)
        return False

    def _report(
        self,
        code: str,
        node: cst.CSTNode,
        message: str,
        detail: Mapping[str, str | int] | None = None,
    ) -> None:
        line = self.get_metadata(PositionProvider, node, _NO_POSITION).start.line
        function = self._function
        key = (code, line, function or "")
        self._findings.setdefault(
            key,
            Finding(
                code=code,
                message=message,
                site=Site(self._path, line, function),
                detail=detail or {},
            ),
        )

    def _names(self, node: cst.CSTNode) -> frozenset[str]:
        """The qualified names `node` resolves to, falling back to how it is spelled.

        The fallback is what recognizes a call whose module the file never imports, which happens
        in a conftest that reaches its dependency through a star import.
        """
        resolved = {name.name for name in self.get_metadata(QualifiedNameProvider, node, ())}
        if resolved:
            return frozenset(resolved)
        spelled = _dotted(node.func if isinstance(node, cst.Call) else node)
        return frozenset({spelled}) if spelled else frozenset()

    def _is_fixture(self, node: cst.BaseExpression, name: str) -> bool:
        """Whether `node` is the fixture named `name`, rather than a local of the same name."""
        return isinstance(node, cst.Name) and node.value == name and self._takes(name)

    def _takes(self, name: str) -> bool:
        """Whether `name` here is the fixture pytest injects, rather than an ordinary parameter.

        The innermost `def` that declares the parameter decides, since it shadows any above it.
        Only `request` also asks who calls that `def`: it is the one of these names a suite spells
        for an object of its own — an HTTP request, a web framework's — and a suite full of them
        otherwise reads as one holding pytest's in every handler it writes. The rest are pytest's
        alone, so a helper the suite hands one to is still where the hazard is written.
        """
        for frame in reversed(self._stack):
            if frame.is_function and name in frame.params:
                return frame.injects if name == REQUEST else True
        return False

    def _binds(self, name: str) -> bool:
        """Whether an enclosing function has a name of its own, so a module-level one is hidden."""
        return any(
            frame.is_function and (name in frame.params or name in frame.locals)
            for frame in self._stack
        )

    def _parent_attribute(self, node: cst.CSTNode) -> str | None:
        parent = self.get_metadata(ParentNodeProvider, node, None)
        return parent.attr.value if isinstance(parent, cst.Attribute) else None

    def _enter_block(self, keyword: str) -> None:
        self._stack[-1].blocks.append(keyword)

    def _leave_block(self) -> None:
        self._stack[-1].blocks.pop()

    @property
    def _function(self) -> str | None:
        parts = [frame.name for frame in self._stack if frame.name]
        return ".".join(parts) if parts else None

    @property
    def _in_function(self) -> bool:
        return any(frame.is_function for frame in self._stack)

    @property
    def _in_async(self) -> bool:
        for frame in reversed(self._stack):
            if frame.is_function:
                return frame.is_async
        return False


def _is_fixture_def(node: cst.FunctionDef) -> bool:
    """Whether a `@pytest.fixture`, however it is spelled, is on this definition."""
    for decorator in node.decorators:
        target = decorator.decorator
        if isinstance(target, cst.Call):
            target = target.func
        if isinstance(target, cst.Attribute) and target.attr.value == "fixture":
            return True
        if isinstance(target, cst.Name) and target.value == "fixture":
            return True
    return False


def _declares_receiver(node: cst.FunctionDef) -> bool:
    """Whether this `def` binds a `self` of its own, which shadows any `self` above it."""
    return any(param.name.value == "self" for param in node.params.params)


def _class_attributes(node: cst.ClassDef) -> frozenset[str]:
    """The names a class body binds directly: its methods, its nested classes, its assignments.

    An annotation with no value binds nothing — `client: Client` in a class body declares what an
    *instance* attribute will be, and reading it off the class raises.
    """
    found: set[str] = set()
    for statement in node.body.body:
        match statement:
            case cst.FunctionDef() | cst.ClassDef():
                found.add(statement.name.value)
            case cst.SimpleStatementLine(body=body):
                for small in body:
                    if isinstance(small, cst.Assign | cst.AnnAssign):
                        found |= _class_assigned(small)
            case _:
                pass
    return frozenset(found)


def _class_assigned(statement: cst.Assign | cst.AnnAssign) -> frozenset[str]:
    """The plain names one assignment in a class body binds on the class itself."""
    if isinstance(statement, cst.AnnAssign):
        targets = [statement.target] if statement.value is not None else []
    else:
        targets = [target.target for target in statement.targets]
    return frozenset(target.value for target in targets if isinstance(target, cst.Name))


def _module_level_names(module: cst.Module) -> frozenset[str]:
    """The names an assignment binds at module level, which every body in the file can write."""
    names: set[str] = set()
    for statement in module.body:
        if not isinstance(statement, cst.SimpleStatementLine):
            continue
        for small in statement.body:
            if isinstance(small, cst.Assign):
                targets = [target.target for target in small.targets]
            elif isinstance(small, cst.AnnAssign | cst.AugAssign):
                targets = [small.target]
            else:
                continue
            for target in targets:
                names.update(_bound_names(target))
    return frozenset(names)


def _param_names(params: cst.Parameters) -> frozenset[str]:
    """Every name `params` binds, which is how a fixture object is told from a local."""
    names = {
        param.name.value
        for param in (*params.posonly_params, *params.params, *params.kwonly_params)
    }
    for star in (params.star_arg, params.star_kwarg):
        if isinstance(star, cst.Param):
            names.add(star.name.value)
    return frozenset(names)


def _assigned_names(function: cst.FunctionDef) -> frozenset[str]:
    """Every name `function`'s own body binds, nested functions excluded.

    A test that assigns `CACHE = {}` before writing to it is writing its own dictionary, not the
    module's, however the two are spelled.
    """
    collector = _Bindings()
    function.body.visit(collector)
    return frozenset(collector.names)


class _Bindings(cst.CSTVisitor):
    """The names one function body binds, gathered without descending into nested definitions."""

    def __init__(self) -> None:
        super().__init__()
        self.names: set[str] = set()

    def visit_Assign(self, node: cst.Assign) -> None:
        for target in node.targets:
            self.names.update(_bound_names(target.target))

    def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
        self.names.update(_bound_names(node.target))

    def visit_AugAssign(self, node: cst.AugAssign) -> None:
        self.names.update(_bound_names(node.target))

    def visit_NamedExpr(self, node: cst.NamedExpr) -> None:
        self.names.update(_bound_names(node.target))

    def visit_For(self, node: cst.For) -> None:
        self.names.update(_bound_names(node.target))

    def visit_AsName(self, node: cst.AsName) -> None:
        if isinstance(node.name, cst.BaseExpression):
            self.names.update(_bound_names(node.name))

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        return False

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        return False


def _bound_names(target: cst.BaseExpression) -> list[str]:
    if isinstance(target, cst.Tuple | cst.List):
        return [
            name
            for element in target.elements
            for name in _bound_names(element.value)  # `A, B = ...` binds both
        ]
    name = _bound_name(target)
    return [name] if name else []


def _bound_name(target: cst.BaseExpression) -> str | None:
    return target.value if isinstance(target, cst.Name) else None


def _root_name(node: cst.BaseExpression) -> str | None:
    """The name at the left of an attribute or subscript chain, if the chain starts with one."""
    while isinstance(node, cst.Attribute | cst.Subscript):
        node = node.value
    return node.value if isinstance(node, cst.Name) else None


def _dotted(node: cst.CSTNode) -> str | None:
    """`a.b.c` for a chain of plain names, and `None` for anything else."""
    parts: list[str] = []
    while isinstance(node, cst.Attribute):
        parts.append(node.attr.value)
        node = node.value
    if not isinstance(node, cst.Name):
        return None
    parts.append(node.value)
    return ".".join(reversed(parts))


def approx_nested(argument: cst.BaseExpression) -> cst.BaseExpression | None:
    """The first literal nested one level inside `argument`, if `argument` is a list, tuple, or
    dict literal and one of its own elements is itself a list, tuple, dict, or set literal.

    A comprehension's runtime shape cannot be inspected this way, so it is left unchecked. Shared
    with `voci_migrate.convert.rules.bodies._Approx`, which refuses converting the same shape.
    """
    if isinstance(argument, cst.List | cst.Tuple):
        values: Iterable[cst.BaseExpression] = (
            element.value for element in argument.elements if isinstance(element, cst.Element)
        )
    elif isinstance(argument, cst.Dict):
        values = (
            element.value for element in argument.elements if isinstance(element, cst.DictElement)
        )
    else:
        return None
    return next((value for value in values if isinstance(value, _APPROX_NESTED_LITERALS)), None)


def _positional(node: cst.Call) -> list[cst.Arg]:
    return [arg for arg in node.args if arg.keyword is None and arg.star == ""]


def _keyword(node: cst.Call, name: str) -> cst.Arg | None:
    for arg in node.args:
        if arg.keyword is not None and arg.keyword.value == name:
            return arg
    return None


# The keyword `pytest.skip`/`pytest.fail` accept for their reason -- `fail`'s `msg=` is a
# deprecated alias for `reason=`. Exported for `convert.rules.bodies._Imperative` to share: this
# module is what decides VC214's shape (this file is already the lower layer -- `bodies.py`
# imports `APPROX_KINDS`/`approx_nested` from here too), so the rewrite rule reads the same
# eligibility this scan reports against, rather than a second hand-kept copy of it.
IMPERATIVE_REASON_KEYWORDS: Mapping[str, frozenset[str]] = {
    "pytest.skip": frozenset({"reason"}),
    "pytest.fail": frozenset({"reason", "msg"}),
}


def imperative_reason_args(node: cst.Call, name: str) -> tuple[cst.BaseExpression, ...] | None:
    """`node` -- a bare `pytest.skip`/`pytest.fail` call -- as the zero or one reason expression
    VC214's rewrite passes `voci.Skipped`/`voci.Failed` positionally, or `None` where the shape
    is left alone: more than one candidate reason (a positional alongside a keyword, or both of
    `fail`'s two spellings), a keyword neither name accepts (`allow_module_level=`, `pytrace=`),
    or a `*`/`**` unpack that might carry one of either."""
    given = _positional(node)
    allowed = IMPERATIVE_REASON_KEYWORDS[name]
    all_keywords = frozenset(arg.keyword.value for arg in node.args if arg.keyword is not None)
    reason_keywords = [kw for kw in sorted(allowed) if _keyword(node, kw) is not None]
    unpacked = any(arg.star in ("*", "**") for arg in node.args)
    if (
        unpacked
        or bool(all_keywords - allowed)
        or len(given) > 1
        or bool(given) + len(reason_keywords) > 1
    ):
        return None
    if given:
        return (given[0].value,)
    if reason_keywords:
        arg = _keyword(node, reason_keywords[0])
        assert arg is not None
        return (arg.value,)
    return ()


def _condition(node: cst.Call, positional: list[cst.Arg]) -> cst.BaseExpression | None:
    """A mark's condition, written either first or as `condition=`."""
    if positional:
        return positional[0].value
    keyword = _keyword(node, "condition")
    return keyword.value if keyword is not None else None


def _is_false(node: cst.BaseExpression) -> bool:
    return isinstance(node, cst.Name) and node.value == "False"


def _is_integer(node: cst.BaseExpression) -> bool:
    return isinstance(node, cst.Integer)


def _is_mock_patch(names: Iterable[str]) -> bool:
    """Whether `names` is `mock.patch` or one of its `object`, `dict` and `multiple` forms."""
    return any(
        name == root or name.startswith(root + ".")
        for name in names
        for root in ("unittest.mock.patch", "mock.patch")
    )


def _blocking_name(names: Iterable[str]) -> str | None:
    for name in sorted(names):
        if name in _BLOCKING or name.startswith("requests."):
            return name
    return None


def _faker_target(node: cst.BaseExpression) -> bool:
    """Whether `.seed*` is being called on a Faker, however that Faker was reached."""
    reached = node.func if isinstance(node, cst.Call) else node
    dotted = _dotted(reached)
    return dotted is not None and dotted.split(".")[-1] in _FAKERS


def _is_binding(parent: cst.CSTNode | None, node: cst.Name) -> bool:
    """Whether `node` is naming something at `parent` rather than reading it."""
    if isinstance(parent, cst.Param | cst.FunctionDef | cst.ClassDef | cst.NameItem | cst.AsName):
        return parent.name is node
    if isinstance(parent, cst.AssignTarget | cst.Del):
        return parent.target is node
    if isinstance(parent, cst.AugAssign | cst.AnnAssign | cst.NamedExpr):
        return parent.target is node
    if isinstance(parent, cst.Arg):
        return parent.keyword is node
    if isinstance(parent, cst.Attribute):
        return parent.attr is node
    return False


def _arguments(node: cst.Call) -> str:
    """A call's arguments as written, without the parentheses."""
    written = []
    for arg in node.args:
        value = " ".join(_EMPTY_MODULE.code_for_node(arg.value).split())
        written.append(f"{arg.keyword.value}={value}" if arg.keyword else f"{arg.star}{value}")
    return _shorten(", ".join(written))


def _render(node: cst.CSTNode) -> str:
    """`node` as one line of source, shortened to keep a message readable."""
    return _shorten(" ".join(_EMPTY_MODULE.code_for_node(node).split()))


def _shorten(text: str, limit: int = 48) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _key(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.as_posix()
