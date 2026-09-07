"""Which tests a piece of source runs for.

A hazard is rarely written where it bites: a `monkeypatch` call sits in a fixture two directories
away from the tests that inherit it, and a module-level patch reaches every test in its file. The
percent-of-suite-serialized estimate is only worth quoting if a finding's blast radius is the set
of tests that actually execute that line, so this maps a source location back onto node ids using
the dump's own fixture closures.

Attribution is deliberately generous: a location that matches no test and no fixture is charged to
every test in its file, since over-reporting a hazard costs a reader an inspection while
under-reporting it costs them a red suite at concurrency.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from velox_migrate.model import FixtureDef, GroundTruth


class Reach:
    """The tests each file, test function, class and fixture in a suite reaches."""

    def __init__(self, ground_truth: GroundTruth) -> None:
        self._all: tuple[str, ...] = tuple(item.nodeid for item in ground_truth.items)
        by_file: dict[str, list[str]] = {}
        by_qualname: dict[tuple[str, str], list[str]] = {}
        by_class: dict[tuple[str, str], list[str]] = {}
        qualnames_by_file: dict[str, set[str]] = {}
        fixture_tests: dict[str, set[str]] = {}
        fixtures_in_file: dict[str, set[str]] = {}
        location: dict[tuple[str, str], set[str]] = {}

        for item in ground_truth.items:
            if item.path is not None:
                by_file.setdefault(item.path, []).append(item.nodeid)
                qualname = f"{item.cls}.{item.originalname}" if item.cls else item.originalname
                by_qualname.setdefault((item.path, qualname), []).append(item.nodeid)
                qualnames_by_file.setdefault(item.path, set()).add(qualname)
                if item.cls:
                    by_class.setdefault((item.path, item.cls), []).append(item.nodeid)
            for fixture in item.walk():
                fixture_tests.setdefault(fixture.key, set()).add(item.nodeid)

        for fixture in ground_truth.fixture_defs.values():
            file, qualname = fixture.func.file, fixture.func.qualname
            if file is None:
                continue
            fixtures_in_file.setdefault(file, set()).add(fixture.key)
            if qualname is not None:
                location.setdefault((file, qualname), set()).add(fixture.key)

        self._by_file = {file: tuple(nodeids) for file, nodeids in by_file.items()}
        self._by_qualname = {key: tuple(nodeids) for key, nodeids in by_qualname.items()}
        self._by_class = {key: tuple(nodeids) for key, nodeids in by_class.items()}
        self._known_tests = {file: frozenset(names) for file, names in qualnames_by_file.items()}
        self._fixture_tests = fixture_tests
        self._fixtures_in_file = fixtures_in_file
        self._location = location

    @property
    def tests(self) -> tuple[str, ...]:
        """Every collected test, in collection order."""
        return self._all

    def tests_in_file(self, file: str) -> tuple[str, ...]:
        return self._by_file.get(file, ())

    def known_tests(self, file: str) -> frozenset[str]:
        """The qualnames -- `Class.method` or a bare function name -- pytest actually collected as
        tests in `file`, however its own `python_functions` pattern is spelled. Empty for a file
        with none, which is as good an answer as a populated set: `sources.scan` reads either as
        the dump's own word on what counts, rather than guessing from a name prefix."""
        return self._known_tests.get(file, frozenset())

    def tests_of_fixture(self, key: str) -> tuple[str, ...]:
        """The tests whose fixture closure reaches the definition keyed `key`."""
        return tuple(sorted(self._fixture_tests.get(key, ())))

    def tests_of_fixtures(self, keys: Iterable[str]) -> tuple[str, ...]:
        found: set[str] = set()
        for key in keys:
            found |= self._fixture_tests.get(key, set())
        return tuple(sorted(found))

    def tests_at(self, file: str | None, function: str | None) -> tuple[str, ...]:
        """The tests that run the code at `file`, inside `function`.

        `function` is a qualified name within the file — a test, a test method, a class, or a
        fixture factory. `None` means module level. A name that matches nothing falls back to the
        file, and a file that holds no tests falls back to the fixtures defined in it and, for a
        `conftest.py`, to everything under its directory.
        """
        if file is None:
            return ()
        # Innermost name first, then each name it is nested in: a function defined inside a test
        # runs for that test, and a `setup_method` runs for its class's tests.
        enclosing = function
        while enclosing:
            exact = self._by_qualname.get((file, enclosing))
            if exact:
                return exact
            fixtures = self._location.get((file, enclosing))
            if fixtures:
                return self.tests_of_fixtures(fixtures)
            in_class = self._by_class.get((file, enclosing))
            if in_class:
                return in_class
            enclosing, _, _ = enclosing.rpartition(".")
        # A name that matches nothing, such as a module-level helper, is charged to the file.
        return self._file_reach(file)

    def tests_under(self, node: str) -> tuple[str, ...]:
        """The tests a node id covers: a directory, a module, a class, or the whole session."""
        if node in ("", ".", "/"):
            return self._all
        return tuple(
            nodeid
            for nodeid in self._all
            if nodeid == node or nodeid.startswith((f"{node}/", f"{node}::"))
        )

    def _file_reach(self, file: str) -> tuple[str, ...]:
        own = self._by_file.get(file)
        if own:
            return own
        through_fixtures = self.tests_of_fixtures(self._fixtures_in_file.get(file, ()))
        if through_fixtures:
            return through_fixtures
        if file.endswith("conftest.py"):
            directory = file.rsplit("/", 1)[0] if "/" in file else ""
            return self.tests_under(directory)
        return ()


def fixtures_by_file(ground_truth: GroundTruth) -> Mapping[str, tuple[FixtureDef, ...]]:
    """Every fixture definition the dump carries, grouped by the file its factory is written in."""
    grouped: dict[str, list[FixtureDef]] = {}
    for fixture in ground_truth.fixture_defs.values():
        if fixture.func.file is not None:
            grouped.setdefault(fixture.func.file, []).append(fixture)
    return {file: tuple(fixtures) for file, fixtures in grouped.items()}
