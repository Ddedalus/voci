"""`voci._affected.environment.env_key`: M4's real environment key."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from voci._affected.environment import env_key
from voci._config import Config


def _key(
    rootdir: Path,
    config: Config | None = None,
    *,
    concurrency: int = 4,
    timeout: float | None = None,
    filterwarnings: tuple[str, ...] = (),
    assert_mode: str = "assertions",
) -> str:
    return env_key(
        rootdir,
        config or Config(rootdir=rootdir),
        concurrency=concurrency,
        timeout=timeout,
        filterwarnings=filterwarnings,
        assert_mode=assert_mode,
    )


def test_env_key_is_stable_within_one_interpreter_and_config(tmp_path: Path) -> None:
    assert _key(tmp_path) == _key(tmp_path)


def test_env_key_names_the_running_interpreter(tmp_path: Path) -> None:
    key = _key(tmp_path)
    assert sys.implementation.name in key
    assert sys.platform in key


def test_env_key_changes_when_concurrency_changes(tmp_path: Path) -> None:
    assert _key(tmp_path, concurrency=1) != _key(tmp_path, concurrency=8)


def test_env_key_changes_when_timeout_changes(tmp_path: Path) -> None:
    assert _key(tmp_path, timeout=None) != _key(tmp_path, timeout=30.0)


def test_env_key_changes_when_assert_mode_changes(tmp_path: Path) -> None:
    assert _key(tmp_path, assert_mode="assertions") != _key(tmp_path, assert_mode="plain")


def test_env_key_changes_when_filterwarnings_changes(tmp_path: Path) -> None:
    assert _key(tmp_path, filterwarnings=()) != _key(tmp_path, filterwarnings=("error",))


def test_env_key_changes_when_a_tool_voci_key_changes(tmp_path: Path) -> None:
    plain = Config(rootdir=tmp_path)
    with_timeout = Config(rootdir=tmp_path, timeout=30.0)
    assert _key(tmp_path, plain) != _key(tmp_path, with_timeout)


def test_env_key_ignores_where_the_config_was_found(tmp_path: Path) -> None:
    """`source`/`anchored`/`git_root` say *where* `[tool.voci]` was found, not what it says --
    the store is already per-repository, so these mustn't make an otherwise-identical config look
    like a different environment."""
    bare = Config(rootdir=tmp_path)
    anchored = Config(
        rootdir=tmp_path, source=tmp_path / "pyproject.toml", anchored=True, git_root=tmp_path
    )
    assert _key(tmp_path, bare) == _key(tmp_path, anchored)


def test_env_key_changes_when_a_locale_variable_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LANG", raising=False)
    without = _key(tmp_path)
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    with_lang = _key(tmp_path)
    assert without != with_lang


def test_env_key_includes_a_first_party_extension_module(tmp_path: Path) -> None:
    without = _key(tmp_path)
    extension = tmp_path / "_native.so"
    extension.write_bytes(b"not really an extension, just bytes to hash")
    with_extension = _key(tmp_path)
    assert without != with_extension


def test_env_key_ignores_an_extension_module_outside_rootdir(tmp_path: Path) -> None:
    """A sanity check that `_extension_fingerprint` never scans past `rootdir` at all -- a
    sibling directory's `.so` is exactly as irrelevant to this key as a third-party one under
    site-packages, which `is_first_party` excludes the same way."""
    outside = tmp_path.parent / f"{tmp_path.name}-sibling-extension.so"
    try:
        outside.write_bytes(b"outside rootdir entirely")
        assert _key(tmp_path) == _key(tmp_path)
    finally:
        outside.unlink(missing_ok=True)
