"""Tests for voci._run.coverage: carrying a `coverage run` into `@voci.isolated`
subprocesses and back.

The unit tests stand a `Coverage` instance in for the one a real `coverage run` would have
started (constructed, never started -- starting a second one inside this suite would trace the
suite itself), and go through the same public entry points coverage.py's own subprocess support
uses: a serialized config read back through `Coverage(config_file=":data:...")`, exactly as
`coverage.process_startup` reads what `subprocess_env` writes. The last test is the real thing
end to end: `coverage run -m voci` over a project whose only caller of one function is an
isolated test.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import coverage
import pytest
from _support import Project

from voci._run import coverage as _coverage

#: The prefix `Coverage.__init__` reads a serialized config back through, rather than as a path
#: to a file on disk. Spelled out here because it is what `subprocess_env`'s output is *for*.
_DATA_PREFIX = ":data:"


def _parent(tmp_path: Path, **kwargs: object) -> coverage.Coverage:
    """A `Coverage` standing in for the one measuring the parent run. `config_file=False` keeps
    voci's own `pyproject.toml` out of it, so these tests read what they set and nothing else.
    """
    return coverage.Coverage(config_file=False, data_file=str(tmp_path / "parent"), **kwargs)  # type: ignore[bad-argument-type]


def _child_config(env: dict[str, str]) -> coverage.Coverage:
    """The configuration a subprocess handed `env` would start under."""
    return coverage.Coverage(config_file=_DATA_PREFIX + env["COVERAGE_PROCESS_CONFIG"])


def _write_data(path: Path, lines: dict[str, list[int]]) -> None:
    """A coverage data file at `path`, as a subprocess's own `atexit` save would leave it."""
    data = coverage.CoverageData(basename=str(path))
    data.add_lines(lines)
    data.write()


def _unexpected(text: str) -> None:
    """`note` for the paths that have nothing to report: a measurement problem in one of these
    is the test's own failure."""
    raise AssertionError(f"unexpected note: {text}")


def test_subprocess_env_is_none_when_nothing_is_measuring(tmp_path: Path) -> None:
    """The common case, and the one that has to cost nothing: this suite does not run under
    `coverage run`, so there is no measurement to extend and the subprocess inherits the
    parent's environment untouched."""
    assert _coverage.subprocess_env(tmp_path / "child.coverage", note=_unexpected) is None


def test_subprocess_env_points_the_subprocess_at_its_own_data_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its own, and in parallel mode: an isolated test that spawns processes of its own has
    several of them measuring under this one environment, and a single data file between them
    would have their saves overwriting each other."""
    parent = _parent(tmp_path)
    monkeypatch.setattr(_coverage, "_active", lambda: parent)
    child_data = tmp_path / "child.coverage"

    env = _coverage.subprocess_env(child_data, note=_unexpected)

    assert env is not None
    config = _child_config(env).config
    assert config.data_file == str(child_data)
    assert config.parallel is True


def test_subprocess_env_carries_the_parents_settings_across(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whole-config, not a hand-picked subset: `branch` disagreeing across the boundary makes the
    child's data unmergeable (arcs and lines don't combine), and a `source` the child doesn't
    know about would credit the report with files the user asked to leave out."""
    parent = _parent(tmp_path, branch=True, source=[str(tmp_path / "src")], omit=["*/vendor/*"])
    monkeypatch.setattr(_coverage, "_active", lambda: parent)

    env = _coverage.subprocess_env(tmp_path / "child.coverage", note=_unexpected)

    assert env is not None
    config = _child_config(env).config
    assert config.branch is True
    assert config.source == [str(tmp_path / "src")]
    assert config.run_omit == ["*/vendor/*"]


def test_subprocess_env_leaves_the_parents_own_config_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy, because the parent goes on measuring under this config for the rest of the run --
    writing the child's data file into it would redirect the whole suite's measurement."""
    parent = _parent(tmp_path)
    monkeypatch.setattr(_coverage, "_active", lambda: parent)

    _coverage.subprocess_env(tmp_path / "child.coverage", note=_unexpected)

    assert parent.config.data_file == str(tmp_path / "parent")


def test_subprocess_env_reports_a_coverage_that_cannot_carry_its_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """coverage.py older than 7.10 has no `serialize`. The parent process is measured either way,
    so this is a note rather than a failure -- but a silent one would leave a report claiming
    isolated tests executed nothing."""

    class _OldConfig:
        def copy(self) -> _OldConfig:
            return self

    class _OldCoverage:
        config = _OldConfig()

    monkeypatch.setattr(_coverage, "_active", lambda: _OldCoverage())
    notes: list[str] = []

    assert _coverage.subprocess_env(tmp_path / "child.coverage", note=notes.append) is None
    assert "7.10 or newer" in "".join(notes)


def test_start_in_subprocess_starts_nothing_without_the_environment() -> None:
    """Running the worker by hand, or under a parent that isn't measuring: no environment, no
    measurement, and nothing left started behind this call."""
    _coverage.start_in_subprocess()

    assert coverage.Coverage.current() is None


def test_harvest_merges_a_subprocess_data_file_into_the_live_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the round trip: lines only the subprocess executed end up in the data
    file the parent is still writing, with no `coverage combine` step in between."""
    measured = str(tmp_path / "module.py")
    child_file = tmp_path / "child.coverage"
    _write_data(child_file, {measured: [3, 4]})

    parent = _parent(tmp_path)
    parent.get_data().add_lines({measured: [1]})
    monkeypatch.setattr(_coverage, "_active", lambda: parent)

    _coverage.harvest(child_file, note=_unexpected)

    assert parent.get_data().lines(measured) == [1, 3, 4]


def test_harvest_merges_every_process_that_measured_under_one_data_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parallel mode means the names on disk are the one `subprocess_env` was given plus a
    per-process suffix, and an isolated test that spawns its own processes leaves several. All
    of them are that test's coverage."""
    measured = str(tmp_path / "module.py")
    child_file = tmp_path / "child.coverage"
    _write_data(Path(f"{child_file}.host.101.xxx"), {measured: [3]})
    _write_data(Path(f"{child_file}.host.102.yyy"), {measured: [4]})
    # A sibling test's data, in the same scratch directory, which this harvest must leave alone.
    _write_data(tmp_path / "other.coverage.host.103.zzz", {measured: [9]})

    parent = _parent(tmp_path)
    monkeypatch.setattr(_coverage, "_active", lambda: parent)

    _coverage.harvest(child_file, note=_unexpected)

    assert parent.get_data().lines(measured) == [3, 4]


def test_harvest_ignores_a_data_file_that_was_never_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a subprocess that was killed, or crashed before its `atexit` save, leaves behind.
    Whatever went wrong is already the test's own reported failure."""
    monkeypatch.setattr(_coverage, "_active", lambda: _parent(tmp_path))

    _coverage.harvest(tmp_path / "never-written.coverage", note=_unexpected)


def test_harvest_does_nothing_when_nothing_is_measuring(tmp_path: Path) -> None:
    child_file = tmp_path / "child.coverage"
    _write_data(child_file, {str(tmp_path / "module.py"): [1]})

    _coverage.harvest(child_file, note=_unexpected)


def test_harvest_reports_unreadable_data_rather_than_failing_the_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A data file that exists but can't be read says nothing about the test that just passed
    inside that subprocess, so it must not turn that test's result into an error. `note` is the
    channel for it: raising here, or warning under `filterwarnings = ["error"]`, would escape
    into `run_suite`'s dispatch and take the run down."""
    child_file = tmp_path / "child.coverage"
    child_file.write_bytes(b"not a coverage database")
    monkeypatch.setattr(_coverage, "_active", lambda: _parent(tmp_path))
    notes: list[str] = []

    _coverage.harvest(child_file, note=notes.append)

    assert "could not merge coverage data" in "".join(notes)


def test_coverage_run_measures_lines_only_an_isolated_test_reaches(project: Project) -> None:
    """End to end, the requirement the rest of this module exists for: one `coverage run -m
    voci`, one data file, and the lines executed inside a `@voci.isolated` subprocess are in
    it -- no `--cov-append`, no `coverage combine`.
    """
    project.write(
        "lib.py",
        "def in_process() -> str:\n"
        "    return 'in-process'\n"
        "\n"
        "\n"
        "def only_isolated() -> str:\n"
        "    return 'isolated'\n"
        "\n"
        "\n"
        "def never_called() -> str:\n"
        "    return 'never'\n",
    )
    project.write("pyproject.toml", '[tool.voci]\ntestpaths = ["tests"]\n')
    project.write(
        "tests/test_both.py",
        "import voci\n"
        "from lib import in_process, only_isolated\n"
        "\n"
        "async def test_here():\n"
        "    assert in_process() == 'in-process'\n"
        "\n"
        "@voci.isolated\n"
        "async def test_over_there():\n"
        "    assert only_isolated() == 'isolated'\n",
    )

    completed = subprocess.run(
        [sys.executable, "-m", "coverage", "run", "--source=lib", "-m", "voci"],
        cwd=project.root,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    data = coverage.CoverageData(basename=str(project.root / ".coverage"))
    data.read()
    lines = data.lines(str(project.root / "lib.py")) or []
    # 2 is `in_process`'s body (the parent process), 6 is `only_isolated`'s (the subprocess),
    # and 10 is `never_called`'s -- there to prove this is measurement rather than a data file
    # that credits every line in the module.
    assert 2 in lines
    assert 6 in lines
    assert 10 not in lines
