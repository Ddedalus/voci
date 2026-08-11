"""Fixtures shared across the whole suite. Plain factories live in `_support.py`."""

from __future__ import annotations

from pathlib import Path

import pytest

from _support import Project


@pytest.fixture
def project(tmp_path: Path) -> Project:
    """A `Project` rooted at `tmp_path`."""
    return Project(tmp_path)


@pytest.fixture
def chdir_project(project: Project, monkeypatch: pytest.MonkeyPatch) -> Project:
    """`project`, with the process cwd changed to its root for the test's duration."""
    monkeypatch.chdir(project.root)
    return project
