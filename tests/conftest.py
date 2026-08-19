"""Fixtures shared across the whole suite. Plain factories live in `_support.py`."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from _support import Project

from velox._builtins import capture as _capture


@pytest.fixture(scope="session", autouse=True)
def velox_basetemp_root(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Give the `main()` calls throughout this suite their own basetemp parent, under
    pytest's own tmp dir, for the session.

    The default parent is one directory per user per machine, shared with every other
    velox on it — including a second run of this suite, whose hundreds of runs would
    sweep this one's live session roots as ordinary leftovers.
    """
    parent = tmp_path_factory.mktemp("velox-basetemp")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_capture, "_basetemp_default_root", lambda: parent)
        yield


@pytest.fixture
def project(tmp_path: Path) -> Project:
    """A `Project` rooted at `tmp_path`."""
    return Project(tmp_path)


@pytest.fixture
def chdir_project(project: Project, monkeypatch: pytest.MonkeyPatch) -> Project:
    """`project`, with the process cwd changed to its root for the test's duration."""
    monkeypatch.chdir(project.root)
    return project
