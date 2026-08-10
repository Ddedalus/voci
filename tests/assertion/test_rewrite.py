"""The rewriter, end to end: soundness under side effects, `await` inside an `assert`,
line-number fidelity, and which modules get rewritten at all.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from velox._rewrite import (
    Config,
    _discover_python_files,
    assertion_context,
    install,
    installed_hook,
    plan,
    uninstall,
)
from velox._vendor.assertion import rewrite as vendored


def test_comparison_gets_an_explanation(rewritten) -> None:
    mod = rewritten("def check():\n    assert [1, 2, 3] == [1, 2, 4]\n")
    with assertion_context(Config()), pytest.raises(AssertionError) as exc:
        mod.check()
    message = str(exc.value)
    assert "assert [1, 2, 3] == [1, 2, 4]" in message
    assert "At index 2 diff: 3 != 4" in message


def test_subexpressions_evaluated_exactly_once(rewritten) -> None:
    """Every subexpression is hoisted into a temp before the condition is tested, so a failing
    assert reports values that were actually computed, not recomputed."""
    mod = rewritten(
        """
        calls = []

        def f():
            calls.append(1)
            return 5

        def check():
            assert f() == 6
        """
    )
    with assertion_context(Config()), pytest.raises(AssertionError):
        mod.check()
    assert mod.calls == [1]


def test_side_effects_run_once_even_in_a_passing_assert(rewritten) -> None:
    mod = rewritten(
        """
        calls = []

        def f():
            calls.append(1)
            return 5

        def check():
            assert f() == 5
        """
    )
    mod.check()
    assert mod.calls == [1]


def test_await_inside_assert(rewritten) -> None:
    mod = rewritten(
        """
        async def value():
            return 41

        async def check():
            assert await value() == 42
        """
    )
    with assertion_context(Config()), pytest.raises(AssertionError) as exc:
        asyncio.run(mod.check())
    assert "assert 41 == 42" in str(exc.value)


def test_await_in_a_boolop_assert(rewritten) -> None:
    mod = rewritten(
        """
        async def truthy():
            return True

        async def falsy():
            return False

        async def check():
            assert await truthy() and await falsy()
        """
    )
    with assertion_context(Config()), pytest.raises(AssertionError):
        asyncio.run(mod.check())


def test_line_numbers_survive_the_rewrite(rewritten) -> None:
    """`ast.copy_location` fixups keep tracebacks pointing at the user's line, not the temps."""
    mod = rewritten(
        """
        def check():
            x = 1

            y = 2

            assert x == y
        """
    )
    with pytest.raises(AssertionError) as exc:
        mod.check()
    tb = exc.value.__traceback__
    assert tb is not None
    while tb.tb_next is not None:
        tb = tb.tb_next
    # `dedent` strips the leading newline, so the assert is on line 7 of the written file.
    assert tb.tb_lineno == 7


def test_decorated_function_keeps_its_own_lines(rewritten) -> None:
    mod = rewritten(
        """
        def deco(fn):
            return fn

        @deco
        def check():
            assert 1 == 2
        """
    )
    with pytest.raises(AssertionError) as exc:
        mod.check()
    assert "assert 1 == 2" in str(exc.value)


def test_explicit_message_is_preserved(rewritten) -> None:
    mod = rewritten("def check():\n    assert 1 == 2, 'custom reason'\n")
    with pytest.raises(AssertionError) as exc:
        mod.check()
    assert "custom reason" in str(exc.value)


def test_non_test_module_under_the_root_is_rewritten(rewritten) -> None:
    """velox rewrites every module discovered under the test roots, not just `test_*.py`
    files and conftests -- so a helper module's own asserts explain themselves too."""
    mod = rewritten(
        "def helper_check(a, b):\n    assert a == b\n",
        name="fixtures",
    )
    with assertion_context(Config()), pytest.raises(AssertionError) as exc:
        mod.helper_check([1], [2])
    assert "At index 0 diff: 1 != 2" in str(exc.value)


def test_rewriter_temps_are_filtered_from_locals() -> None:
    """Without this, every failure shows a wall of `@py_assert*` bindings."""
    from velox._rewrite import iter_user_locals, strip_rewriter_temps

    frame_locals = {
        "resp": object(),
        "@py_assert1": 3,
        "@py_format5": "...",
        "expected": 200,
    }
    assert set(strip_rewriter_temps(frame_locals)) == {"resp", "expected"}
    assert [name for name, _ in iter_user_locals(frame_locals)] == ["resp", "expected"]


class TestInstall:
    def test_install_puts_the_hook_first(self, tmp_path) -> None:
        import sys

        try:
            setup = install([tmp_path], cache_dir=tmp_path / "cache")
            assert setup.mode == "rewrite"
            assert installed_hook() is sys.meta_path[0]
        finally:
            uninstall()
        assert installed_hook() is None

    def test_plain_mode_installs_nothing(self, tmp_path) -> None:
        setup = install([tmp_path], mode="plain", cache_dir=tmp_path / "cache")
        assert setup.mode == "plain"
        assert setup.cache_dir is None
        assert not setup.degraded
        assert installed_hook() is None

    def test_uninstall_is_idempotent(self, tmp_path) -> None:
        install([tmp_path], cache_dir=tmp_path / "cache")
        uninstall()
        uninstall()
        assert installed_hook() is None

    def test_install_is_idempotent(self, tmp_path) -> None:
        """A second `install()` must not push a second hook: two rewrites per import, and two
        competing cache roots, since `set_cache_root` is module-global and the last caller
        wins."""
        try:
            first = install([tmp_path], cache_dir=tmp_path / "cache")
            second = install([tmp_path], cache_dir=tmp_path / "a-different-cache")
            assert second is first
            hooks = [e for e in sys.meta_path if isinstance(e, vendored.AssertionRewritingHook)]
            assert len(hooks) == 1
        finally:
            uninstall()
        assert installed_hook() is None

    def test_install_accepts_a_precomputed_setup(self, tmp_path) -> None:
        """`cli.main` runs `plan` once for the report header and must be able to hand the
        result straight to `install` rather than triggering a second cache probe."""
        setup = plan([tmp_path], cache_dir=tmp_path / "cache")
        try:
            installed = install(setup=setup)
            assert installed is setup
            assert installed_hook() is not None
        finally:
            uninstall()


class TestDiscoverPythonFiles:
    """`_discover_python_files` feeds `_initialpaths` (see `_DiscoveredPaths`), which forces a
    rewrite and defeats the rewriter's own name-based bailout — so anything it finds under a
    virtualenv or vendored tree would get rewritten right along with the user's own tests."""

    def test_prunes_a_directory_containing_a_pyvenv_cfg(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        (project / "tests").mkdir(parents=True)
        (project / "tests" / "test_real.py").write_text("assert 1\n")

        venv = project / "some-venv-name"
        (venv / "lib" / "site").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
        (venv / "lib" / "site" / "sneaky.py").write_text("assert 1\n")

        found = {p.resolve() for p in _discover_python_files([project])}

        assert (project / "tests" / "test_real.py").resolve() in found
        assert not any(venv.resolve() in path.parents for path in found)

    def test_prunes_dot_directories_and_pycache(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        (project / "tests").mkdir(parents=True)
        (project / "tests" / "test_real.py").write_text("assert 1\n")
        (project / ".git" / "hooks").mkdir(parents=True)
        (project / ".git" / "hooks" / "sneaky.py").write_text("assert 1\n")
        (project / "tests" / "__pycache__").mkdir()
        (project / "tests" / "__pycache__" / "cached.py").write_text("assert 1\n")

        found = _discover_python_files([project])

        assert {p.resolve() for p in found} == {(project / "tests" / "test_real.py").resolve()}


def test_plan_rejects_an_unrecognised_mode() -> None:
    """A typo from a programmatic caller, or a config value that never went through argparse's
    `choices=`, must raise rather than silently mean "rewrite"."""
    with pytest.raises(ValueError, match="bogus"):
        plan([], mode="bogus")  # type: ignore[arg-type]
