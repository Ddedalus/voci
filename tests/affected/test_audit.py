"""`voci._affected.audit`: the sys.addaudithook callback behind M4's `data:`/`dir:` dependencies
and its "a process spawn makes its collector untrusted" rule
(`plans/affected-tests-plan.md`, Non-code dependencies design section)."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from voci._affected import audit
from voci._affected.collector import Collector, active


def _install(rootdir: Path, session_start: float | None = None):
    return audit.install(rootdir, time.time() if session_start is None else session_start)


def test_open_in_read_mode_under_rootdir_records_a_data_dependency(tmp_path: Path) -> None:
    data = tmp_path / "fixture.json"
    data.write_text("{}")
    token = _install(tmp_path, session_start=data.stat().st_mtime + 60)
    c = Collector()
    try:
        with active(c):
            data.read_text()
        assert c.data_paths == {str(data.resolve())}
    finally:
        audit.uninstall(token)


def test_open_in_write_mode_records_nothing(tmp_path: Path) -> None:
    data = tmp_path / "out.txt"
    token = _install(tmp_path)
    c = Collector()
    try:
        with active(c):
            data.write_text("hello")
        assert c.data_paths == frozenset()
    finally:
        audit.uninstall(token)


def test_a_py_file_is_never_recorded_as_a_data_dependency(tmp_path: Path) -> None:
    module = tmp_path / "mod.py"
    module.write_text("x = 1\n")
    token = _install(tmp_path, session_start=module.stat().st_mtime + 60)
    c = Collector()
    try:
        with active(c):
            module.read_text()
        assert c.data_paths == frozenset()
    finally:
        audit.uninstall(token)


def test_a_file_written_after_the_session_started_is_never_recorded(tmp_path: Path) -> None:
    """The design section's own "files modified after the session started" exclusion -- an
    output the run itself wrote, not an input it depends on."""
    data = tmp_path / "fixture.txt"
    data.write_text("written after session_start")
    # Well before the file's own mtime, not just-now: a `session_start` a few milliseconds ahead
    # of a write that happens right after it would make this test racy against filesystem mtime
    # granularity, the same reason the sqlite3 test below uses a margin rather than `+1`.
    token = _install(tmp_path, session_start=data.stat().st_mtime - 60)
    c = Collector()
    try:
        with active(c):
            data.read_text()
        assert c.data_paths == frozenset()
    finally:
        audit.uninstall(token)


def test_nothing_is_recorded_with_no_current_collector(tmp_path: Path) -> None:
    data = tmp_path / "fixture.json"
    data.write_text("{}")
    token = _install(tmp_path, session_start=data.stat().st_mtime + 60)
    try:
        data.read_text()  # no active() collector at all
    finally:
        audit.uninstall(token)
    # Nothing to assert on directly -- this must simply not raise, and no collector exists to
    # have recorded anything into.


def test_nothing_is_recorded_once_uninstalled(tmp_path: Path) -> None:
    data = tmp_path / "fixture.json"
    data.write_text("{}")
    token = _install(tmp_path, session_start=data.stat().st_mtime + 60)
    audit.uninstall(token)
    c = Collector()
    with active(c):
        data.read_text()
    assert c.data_paths == frozenset()


def test_os_listdir_under_rootdir_records_a_dir_dependency(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    token = _install(tmp_path)
    c = Collector()
    try:
        with active(c):
            os.listdir(fixtures)
        assert c.dir_paths == {str(fixtures.resolve())}
    finally:
        audit.uninstall(token)


def test_os_scandir_under_rootdir_records_a_dir_dependency(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    token = _install(tmp_path)
    c = Collector()
    try:
        with active(c):
            list(os.scandir(fixtures))
        assert c.dir_paths == {str(fixtures.resolve())}
    finally:
        audit.uninstall(token)


def test_sqlite3_connect_to_a_real_file_records_a_data_dependency(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    sqlite3.connect(db_path).close()  # created before the session starts
    # A generous margin past the file's own mtime, not `+1` -- some filesystems only resolve
    # mtimes to the nearest second, and a bare connect/close can still touch the file.
    token = _install(tmp_path, session_start=db_path.stat().st_mtime + 60)
    c = Collector()
    try:
        with active(c):
            conn = sqlite3.connect(db_path)
            conn.close()
        assert str(db_path.resolve()) in c.data_paths
    finally:
        audit.uninstall(token)


def test_sqlite3_connect_to_memory_records_nothing(tmp_path: Path) -> None:
    token = _install(tmp_path)
    c = Collector()
    try:
        with active(c):
            conn = sqlite3.connect(":memory:")
            conn.close()
        assert c.data_paths == frozenset()
    finally:
        audit.uninstall(token)


def test_a_process_spawn_marks_its_collector_untrusted(tmp_path: Path) -> None:
    token = _install(tmp_path)
    c = Collector()
    try:
        with active(c):
            subprocess.run([sys.executable, "-c", "pass"], check=True)
        assert c.untrusted is not None
        assert "process spawn" in c.untrusted
    finally:
        audit.uninstall(token)


def test_exempt_own_spawn_suppresses_the_untrusted_mark(tmp_path: Path) -> None:
    token = _install(tmp_path)
    c = Collector()
    try:
        with active(c), audit.exempt_own_spawn():
            subprocess.run([sys.executable, "-c", "pass"], check=True)
        assert c.untrusted is None
    finally:
        audit.uninstall(token)


def test_exempt_own_spawn_only_suppresses_for_its_own_duration(tmp_path: Path) -> None:
    token = _install(tmp_path)
    c = Collector()
    try:
        with active(c):
            with audit.exempt_own_spawn():
                pass
            subprocess.run([sys.executable, "-c", "pass"], check=True)
        assert c.untrusted is not None
    finally:
        audit.uninstall(token)
