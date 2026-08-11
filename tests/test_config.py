"""Tests for velox._config: `pyproject.toml` search, `[tool.velox]` validation, and merging."""

from __future__ import annotations

from pathlib import Path

from _support import Project

import pytest
from velox._config import Config, ConfigError, resolve


def test_no_pyproject_anywhere_falls_back_to_defaults(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    config = resolve([project])

    assert config == Config(rootdir=project)


def test_finds_tool_velox_in_the_search_start_itself(tmp_path: Path) -> None:
    Project(tmp_path).write_pyproject(
        "[tool.velox]\ntestpaths = ['tests']\nconcurrency = 8\ntimeout = 30\n",
    )

    config = resolve([tmp_path])

    assert config.rootdir == tmp_path
    assert config.source == tmp_path / "pyproject.toml"
    assert config.testpaths == ("tests",)
    assert config.concurrency == 8
    assert config.timeout == 30.0


def test_walks_upward_from_a_nested_explicit_path(tmp_path: Path) -> None:
    Project(tmp_path).write_pyproject("[tool.velox]\nconcurrency = 5\n")
    nested = tmp_path / "a" / "b" / "test_deep.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("")

    config = resolve([nested])

    assert config.rootdir == tmp_path
    assert config.concurrency == 5


def test_a_plain_pyproject_without_tool_velox_is_skipped(tmp_path: Path) -> None:
    """A `pyproject.toml` with no `[tool.velox]` table is not a match -- the search keeps
    walking upward past it."""
    Project(tmp_path).write_pyproject("[project]\nname = 'unrelated'\n")
    nested = tmp_path / "pkg"
    Project(nested).write_pyproject("[tool.velox]\nconcurrency = 7\n")
    sub = nested / "tests"
    sub.mkdir()

    config = resolve([sub])

    assert config.rootdir == nested
    assert config.concurrency == 7


def test_search_stops_at_git_root_without_config(tmp_path: Path) -> None:
    """The search never walks above the git root, even when nothing was found."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    project = repo / "sub"
    project.mkdir()
    # A `[tool.velox]` above the git root must never be picked up.
    Project(tmp_path).write_pyproject("[tool.velox]\nconcurrency = 99\n")

    config = resolve([project])

    assert config == Config(rootdir=project)


def test_git_root_directory_itself_is_still_checked_for_config(tmp_path: Path) -> None:
    """The git root itself is examined, not skipped."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    Project(repo).write_pyproject("[tool.velox]\nconcurrency = 12\n")
    project = repo / "sub"
    project.mkdir()

    config = resolve([project])

    assert config.rootdir == repo
    assert config.concurrency == 12


def test_no_explicit_paths_searches_from_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Project(tmp_path).write_pyproject("[tool.velox]\nconcurrency = 3\n")
    monkeypatch.chdir(tmp_path)

    config = resolve([])

    assert config.rootdir == tmp_path
    assert config.concurrency == 3


def test_search_start_is_the_common_ancestor_of_multiple_paths(tmp_path: Path) -> None:
    Project(tmp_path).write_pyproject("[tool.velox]\nconcurrency = 2\n")
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()

    config = resolve([left, right])

    assert config.rootdir == tmp_path


def test_a_file_path_searches_from_its_parent_directory(tmp_path: Path) -> None:
    Project(tmp_path).write_pyproject("[tool.velox]\nconcurrency = 6\n")
    test_file = tmp_path / "test_one.py"
    test_file.write_text("")

    config = resolve([test_file])

    assert config.rootdir == tmp_path
    assert config.concurrency == 6


def test_unknown_key_is_a_config_error(tmp_path: Path) -> None:
    Project(tmp_path).write_pyproject("[tool.velox]\nnot_a_real_key = 1\n")

    with pytest.raises(ConfigError, match="not_a_real_key"):
        resolve([tmp_path])


def test_tool_velox_must_be_a_table(tmp_path: Path) -> None:
    Project(tmp_path).write_pyproject("[tool]\nvelox = 'nope'\n")

    with pytest.raises(ConfigError, match="must be a table"):
        resolve([tmp_path])


def test_malformed_toml_is_a_config_error(tmp_path: Path) -> None:
    Project(tmp_path).write_pyproject("[tool.velox\nthis is not valid toml")

    with pytest.raises(ConfigError):
        resolve([tmp_path])


@pytest.mark.parametrize("bad", ["'sixteen'", "true", "16.5", "[1, 2]"])
def test_concurrency_must_be_a_plain_integer(tmp_path: Path, bad: str) -> None:
    Project(tmp_path).write_pyproject(f"[tool.velox]\nconcurrency = {bad}\n")

    with pytest.raises(ConfigError, match="concurrency"):
        resolve([tmp_path])


def test_timeout_accepts_both_int_and_float(tmp_path: Path) -> None:
    Project(tmp_path).write_pyproject("[tool.velox]\ntimeout = 30\n")
    assert resolve([tmp_path]).timeout == 30.0

    Project(tmp_path).write_pyproject("[tool.velox]\ntimeout = 30.5\n")
    assert resolve([tmp_path]).timeout == 30.5


def test_timeout_rejects_a_bool(tmp_path: Path) -> None:
    """TOML `true`/`false` are Python `bool`, a subclass of `int` -- `timeout = true` must be
    a usage error, not silently become `timeout = 1.0`."""
    Project(tmp_path).write_pyproject("[tool.velox]\ntimeout = true\n")

    with pytest.raises(ConfigError, match="timeout"):
        resolve([tmp_path])


def test_testpaths_must_be_a_list_of_strings(tmp_path: Path) -> None:
    Project(tmp_path).write_pyproject("[tool.velox]\ntestpaths = 'tests'\n")

    with pytest.raises(ConfigError, match="testpaths"):
        resolve([tmp_path])

    Project(tmp_path).write_pyproject("[tool.velox]\ntestpaths = [1, 2]\n")

    with pytest.raises(ConfigError, match="testpaths"):
        resolve([tmp_path])


def test_env_must_be_a_table_of_string_to_string(tmp_path: Path) -> None:
    Project(tmp_path).write_pyproject("[tool.velox]\nenv = { FOO = 1 }\n")

    with pytest.raises(ConfigError, match="env"):
        resolve([tmp_path])


def test_env_and_ignore_and_test_file_patterns_round_trip(tmp_path: Path) -> None:
    Project(tmp_path).write_pyproject(
        "[tool.velox]\n"
        "env = { ENVIRONMENT = 'test', DEBUG = '0' }\n"
        "ignore = ['.git', 'vendor']\n"
        "test_file_patterns = ['check_*.py']\n",
    )

    config = resolve([tmp_path])

    assert config.env == {"ENVIRONMENT": "test", "DEBUG": "0"}
    assert config.ignore == (".git", "vendor")
    assert config.test_file_patterns == ("check_*.py",)


def test_watchdog_threshold_is_not_yet_a_known_key(tmp_path: Path) -> None:
    """`watchdog_threshold` is not a recognized key -- an unknown key is a `ConfigError`."""
    Project(tmp_path).write_pyproject("[tool.velox]\nwatchdog_threshold = 1.0\n")

    with pytest.raises(ConfigError, match="watchdog_threshold"):
        resolve([tmp_path])
