"""spec/07 §4.2 and §5 — pyc cache identity, and the cold-start guarantee.

The cache is load-bearing, not an optimisation: rewriting costs 4.6x on a cold import and a
warm pyc load is 154x faster than that (R§2). Two consequences, both tested here.

*Identity*: a cached pyc must never be reused by something that would have generated different
bytecode. Upstream keys on the CPython magic number alone, which is why
`enable_assertion_pass_hook` is a documented footgun — it changes codegen and not the key.

*Availability*: velox is benchmarked cold in CI containers, so an unwritable cache must be a
loud, named, recorded fallback rather than a silent 4.6x on every run.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from velox import _rewrite
from velox._rewrite import ENV_CACHE_DIR, Config, install, plan, resolve_cache_dir, uninstall
from velox._vendor.assertion import rewrite as vendored


class TestCacheKey:
    def test_tag_carries_the_rewriter_revision(self) -> None:
        """A change to velox's vendored codegen must invalidate pycs, not just a CPython bump."""
        assert sys.implementation.cache_tag in vendored.PYTEST_TAG
        assert "velox" in vendored.PYTEST_TAG
        assert "pytest" not in vendored.PYTEST_TAG

    def test_codegen_options_change_the_key(self) -> None:
        """The footgun, closed. Flipping a codegen flag must not reuse the old pycs."""
        default = vendored._velox_pyc_tail(Config())
        with_hook = vendored._velox_pyc_tail(Config({"enable_assertion_pass_hook": True}))
        assert default != with_hook

    def test_non_codegen_options_do_not_change_the_key(self) -> None:
        """Verbosity affects rendering at run time, not generated code — it must not split
        the cache, or every `-v` run would pay a cold import."""
        assert vendored._velox_pyc_tail(Config(verbosity=0)) == vendored._velox_pyc_tail(
            Config(verbosity=2)
        )

    def test_every_declared_codegen_option_is_hashed(self) -> None:
        """Guards the list itself: adding a codegen flag without listing it is the bug."""
        base = vendored._velox_pyc_tail(Config())
        for name in vendored.VELOX_CODEGEN_OPTIONS:
            assert vendored._velox_pyc_tail(Config({name: "sentinel-value"})) != base


class TestPycWriting:
    def test_pyc_is_written_and_reused(self, tmp_path: Path) -> None:
        """The warm path. If this breaks, every run is a cold run and nothing else fails."""
        roots = tmp_path / "suite"
        roots.mkdir()
        (roots / "test_cached.py").write_text("def check():\n    assert 1 == 1\n")
        cache = tmp_path / "cache"

        try:
            install([roots], cache_dir=cache)
            sys.path.insert(0, str(roots))
            __import__("test_cached")
        finally:
            uninstall()
            sys.modules.pop("test_cached", None)
            sys.path.remove(str(roots))

        pycs = list(cache.rglob("test_cached.*.pyc"))
        assert len(pycs) == 1, f"expected exactly one cached pyc, got {pycs}"
        first_mtime = pycs[0].stat().st_mtime_ns

        # Second import: the pyc must be loaded, not rewritten and rewritten again.
        try:
            install([roots], cache_dir=cache)
            sys.path.insert(0, str(roots))
            __import__("test_cached")
        finally:
            uninstall()
            sys.modules.pop("test_cached", None)
            sys.path.remove(str(roots))

        assert pycs[0].stat().st_mtime_ns == first_mtime

    def test_temp_pyc_is_keyed_on_pid_and_thread(self) -> None:
        """Two threads importing different modules must not collide on one temp filename.

        Checked by reading the source of the write path rather than by racing threads: the
        race is real but rare, and a flaky test that passes 99 times out of 100 would be worse
        than no test at all.
        """
        import inspect

        source = inspect.getsource(vendored._write_pyc)
        assert "os.getpid()" in source
        assert "threading.get_ident()" in source

    def test_write_guard_is_thread_local_with_a_lock(self) -> None:
        """Upstream's shared bool was neither per-call-stack nor a mutex."""
        hook = vendored.AssertionRewritingHook(Config())
        assert isinstance(hook._writing_pyc, type(vendored.threading.local()))
        assert isinstance(hook._pyc_write_lock, type(vendored.threading.Lock()))


class TestCacheDirResolution:
    """spec/07 §5.1, in order: explicit flag, env var, platform cache dir, pycache_prefix."""

    def test_explicit_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_CACHE_DIR, str(tmp_path / "from-env"))
        assert resolve_cache_dir(tmp_path / "explicit") == tmp_path / "explicit"

    def test_env_var_is_next(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_CACHE_DIR, str(tmp_path / "from-env"))
        assert resolve_cache_dir() == tmp_path / "from-env"

    def test_platform_cache_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_CACHE_DIR, raising=False)
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.setattr(sys, "platform", "linux")
        assert resolve_cache_dir() == tmp_path / "velox" / "rewrite"

    def test_pycache_prefix_is_the_last_resort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Containers often have no home directory at all; borrow wherever pycs already go."""
        monkeypatch.delenv(ENV_CACHE_DIR, raising=False)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(sys, "pycache_prefix", str(tmp_path / "pycs"))
        assert resolve_cache_dir() == tmp_path / "pycs" / "velox-rewrite"

    def test_expanduser(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_CACHE_DIR, "~/velox-cache")
        assert "~" not in str(resolve_cache_dir())


class TestColdStartGuarantee:
    """spec/07 §5.3 — an unwritable cache must be loud, named, and recorded."""

    def test_writable_cache_stays_in_rewrite_mode(self, tmp_path: Path) -> None:
        setup = plan([], cache_dir=tmp_path / "cache")
        assert setup.mode == "rewrite"
        assert not setup.degraded
        assert setup.cache_dir == tmp_path / "cache"

    def test_probe_creates_and_cleans_up(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        plan([], cache_dir=cache)
        assert cache.is_dir()
        assert list(cache.iterdir()) == [], "the write probe left a marker behind"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can write to anything")
    def test_unwritable_cache_falls_back_to_plain(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o555)
        try:
            setup = plan([], cache_dir=readonly / "nested")
        finally:
            readonly.chmod(0o755)

        assert setup.mode == "plain"
        assert setup.degraded
        assert setup.cache_dir is None

        # Naming the path is the requirement: "rewriting is off" is not actionable.
        warning = capsys.readouterr().err
        assert str(readonly / "nested") in warning
        assert ENV_CACHE_DIR in warning

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can write to anything")
    def test_fallback_is_recorded_in_the_report_header(self, tmp_path: Path) -> None:
        """A benchmark run that silently fell back is a corrupted benchmark."""
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o555)
        try:
            setup = plan([], cache_dir=readonly / "nested", warn=False)
        finally:
            readonly.chmod(0o755)

        header = setup.header_line()
        assert "plain" in header
        assert "fallback" in header

    def test_header_names_the_cache_in_the_happy_path(self, tmp_path: Path) -> None:
        header = plan([], cache_dir=tmp_path / "cache").header_line()
        assert header == f"assertions: rewrite, cache {tmp_path / 'cache'}"

    def test_plain_mode_is_not_degraded(self, tmp_path: Path) -> None:
        """`plain` chosen deliberately reads differently from `plain` fallen back into."""
        setup = plan([], mode="plain", cache_dir=tmp_path / "cache")
        assert setup.header_line() == "assertions: plain"
        assert not setup.degraded

    def test_install_does_not_hook_when_the_probe_fails(self, tmp_path: Path) -> None:
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o555)
        try:
            if os.geteuid() == 0:
                pytest.skip("root can write to anything")
            setup = install([], cache_dir=readonly / "nested")
        finally:
            readonly.chmod(0o755)
            uninstall()
        assert setup.mode == "plain"
        assert _rewrite.installed_hook() is None

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can write to anything")
    def test_install_warns_only_once_given_a_precomputed_setup(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`install` used to call `plan` again internally without forwarding `warn`, so a
        caller that already ran `plan` for the report header (as `cli.main` does) got the same
        two-line stderr warning a second time."""
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o555)
        try:
            setup = plan([], cache_dir=readonly / "nested")
            install(setup=setup)
        finally:
            readonly.chmod(0o755)
            uninstall()

        warning = capsys.readouterr().err
        assert warning.count("assertion rewriting disabled") == 1

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can write to anything")
    def test_install_forwards_warn_on_the_simple_path(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o555)
        try:
            install([], cache_dir=readonly / "nested", warn=False)
        finally:
            readonly.chmod(0o755)
            uninstall()

        assert capsys.readouterr().err == ""


class TestProbeCleanup:
    """A mistyped `--rewrite-cache` used to leave an empty directory tree behind: velox falls
    back to `plain` and never uses it, but `mkdir(parents=True)` had already created it."""

    def test_probe_failure_removes_a_freshly_created_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = tmp_path / "fresh" / "cache"
        original_write_bytes = Path.write_bytes

        def fake_write_bytes(self: Path, data: bytes) -> int:
            if self.name == _rewrite._PROBE_NAME:
                raise OSError("synthetic failure")
            return original_write_bytes(self, data)

        monkeypatch.setattr(Path, "write_bytes", fake_write_bytes)

        problem = _rewrite._probe_writable(cache)

        assert problem is not None
        assert not cache.exists()

    def test_probe_failure_leaves_a_pre_existing_dir_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache = tmp_path / "cache"
        cache.mkdir()
        marker = cache / "already-here.txt"
        marker.write_text("keep me")
        original_write_bytes = Path.write_bytes

        def fake_write_bytes(self: Path, data: bytes) -> int:
            if self.name == _rewrite._PROBE_NAME:
                raise OSError("synthetic failure")
            return original_write_bytes(self, data)

        monkeypatch.setattr(Path, "write_bytes", fake_write_bytes)

        problem = _rewrite._probe_writable(cache)

        assert problem is not None
        assert marker.exists()
