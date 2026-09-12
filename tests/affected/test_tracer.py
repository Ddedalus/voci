"""Tests for voci._affected.tracer: first-party classification, and the sys.monitoring tool
id lifecycle around it."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import CodeType

import pytest

import voci
from voci._affected import tracer
from voci._cache import CACHE_DIR_NAME

# `sys.monitoring` tool ids are process-global, so every test that starts a real `Tracer` is
# marked `@voci.solo` -- run alone, never alongside a sibling that might also reach for ids 3/4.


def _sample() -> int:
    return 1


# is_first_party
# ------------------------------------------------------------------------


def test_a_string_filename_is_not_first_party(tmp_path: Path) -> None:
    assert tracer.is_first_party("<string>", tmp_path) is False


def test_an_empty_filename_is_not_first_party(tmp_path: Path) -> None:
    assert tracer.is_first_party("", tmp_path) is False


def test_a_missing_file_is_not_first_party(tmp_path: Path) -> None:
    missing = tmp_path / "gone.py"
    assert not missing.exists()
    assert tracer.is_first_party(str(missing), tmp_path) is False


def test_a_zip_style_path_is_not_first_party(tmp_path: Path) -> None:
    # A zipimport `co_filename` looks like a real path, but nothing on disk is a file at
    # that exact joined path -- only the zip archive itself is.
    archive = tmp_path / "lib.zip"
    archive.write_bytes(b"not actually a zip")
    member = archive / "pkg" / "mod.py"
    assert tracer.is_first_party(str(member), tmp_path) is False


def test_a_file_under_rootdir_is_first_party(tmp_path: Path) -> None:
    mod = tmp_path / "pkg" / "mod.py"
    mod.parent.mkdir()
    mod.write_text("x = 1\n")
    assert tracer.is_first_party(str(mod), tmp_path) is True


def test_a_file_outside_rootdir_is_not_first_party(tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    outside = other / "mod.py"
    outside.write_text("x = 1\n")
    rootdir = tmp_path / "project"
    rootdir.mkdir()
    assert tracer.is_first_party(str(outside), rootdir) is False


def test_a_file_under_the_cache_dir_is_not_first_party(tmp_path: Path) -> None:
    cached = tmp_path / CACHE_DIR_NAME / "mod.py"
    cached.parent.mkdir()
    cached.write_text("x = 1\n")
    assert tracer.is_first_party(str(cached), tmp_path) is False


def test_a_file_under_sys_prefix_is_not_first_party() -> None:
    # An installed dependency, inside this interpreter's own venv: a real file, and a
    # descendant of the rootdir given here, but excluded anyway.
    import pytest as _pytest

    rootdir = Path(sys.prefix)
    assert tracer.is_first_party(_pytest.__file__, rootdir) is False


def test_a_file_under_sys_base_prefix_is_not_first_party() -> None:
    # The standard library, which sits under base_prefix rather than prefix whenever the
    # two differ (a venv).
    rootdir = Path(sys.base_prefix)
    assert tracer.is_first_party(json.__file__, rootdir) is False


def test_a_file_under_a_nested_venv_is_not_first_party(tmp_path: Path) -> None:
    # pytest-testmon #206: a venv checked out inside rootdir, not the interpreter's own.
    venv = tmp_path / ".venv"
    site_packages = venv / "lib" / "sitepkg"
    site_packages.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    mod = site_packages / "dep.py"
    mod.write_text("x = 1\n")
    assert tracer.is_first_party(str(mod), tmp_path) is False


# Tracer
# ------------------------------------------------------------------------


@voci.solo
def test_start_claims_a_free_tool_id_and_stop_frees_it() -> None:
    t = tracer.Tracer(Path.cwd(), lambda code: None)
    claimed: int | None = None
    try:
        assert t.start() is None
        assert t.tool_id in (3, 4)
        claimed = t.tool_id
    finally:
        t.stop()
    assert t.tool_id is None
    assert claimed is not None

    # Freed for real: claimable again by someone else.
    sys.monitoring.use_tool_id(claimed, "probe")
    sys.monitoring.free_tool_id(claimed)


@voci.solo
def test_start_returns_a_reason_when_no_tool_id_is_free() -> None:
    mon = sys.monitoring
    mon.use_tool_id(3, "held-a")
    mon.use_tool_id(4, "held-b")
    try:
        t = tracer.Tracer(Path.cwd(), lambda code: None)
        reason = t.start()
        assert reason is not None
        assert t.tool_id is None
    finally:
        mon.free_tool_id(3)
        mon.free_tool_id(4)


def test_stop_before_a_successful_start_is_a_noop() -> None:
    t = tracer.Tracer(Path.cwd(), lambda code: None)
    t.stop()  # must not raise, and must not touch sys.monitoring at all
    assert t.tool_id is None


@voci.solo
def test_first_party_code_calls_the_callback_and_third_party_code_does_not() -> None:
    seen: list[CodeType] = []
    t = tracer.Tracer(Path(__file__).parent, seen.append)
    assert t.start() is None
    try:
        _sample()
        json.dumps({"a": 1})
    finally:
        t.stop()
    assert seen == [_sample.__code__]


@voci.solo
def test_calling_first_party_code_twice_calls_the_callback_twice() -> None:
    seen: list[CodeType] = []
    t = tracer.Tracer(Path(__file__).parent, seen.append)
    assert t.start() is None
    try:
        _sample()
        _sample()
    finally:
        t.stop()
    assert seen == [_sample.__code__, _sample.__code__]


@voci.solo
def test_first_party_check_is_cached_per_filename(monkeypatch: pytest.MonkeyPatch) -> None:
    real_is_first_party = tracer.is_first_party
    calls: list[str] = []

    def counting(filename: str, rootdir: Path) -> bool:
        calls.append(filename)
        return real_is_first_party(filename, rootdir)

    monkeypatch.setattr(tracer, "is_first_party", counting)
    t = tracer.Tracer(Path(__file__).parent, lambda code: None)
    assert t.start() is None
    try:
        _sample()
        _sample()
    finally:
        t.stop()
    # The callback looks itself up as a module-level name, so patching it here reaches the
    # calls `_callback` makes -- one per distinct filename, however many times a code object
    # from it runs.
    assert calls.count(_sample.__code__.co_filename) == 1


@voci.solo
def test_after_stop_no_more_callbacks_fire() -> None:
    seen: list[CodeType] = []
    t = tracer.Tracer(Path(__file__).parent, seen.append)
    assert t.start() is None
    _sample()
    t.stop()
    seen.clear()

    _sample()

    assert seen == []
