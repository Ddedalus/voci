"""Tests for the PEP 657 fallback: explanations for asserts in unrewritten code.

pytest rewrites test files only, so an `assert` in a helper module raises a bare
`AssertionError` with nothing under it. PEP 657 column spans close most of that gap for
free, and without running user code a second time to re-evaluate the expression.
"""

from __future__ import annotations

import linecache
import textwrap
from pathlib import Path

import pytest
from velox._assertions.pep657 import explain_assertion, source_at


@pytest.fixture
def unrewritten(tmp_path: Path):
    """Import a module without the rewriting hook."""
    import importlib.util
    import sys

    def build(source: str, name: str = "unrewritten_mod"):
        path = tmp_path / f"{name}.py"
        path.write_text(textwrap.dedent(source))
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(name, None)
        return module

    return build


def _explain(fn, *args) -> str | None:
    try:
        fn(*args)
    except AssertionError as exc:
        return explain_assertion(exc)
    raise AssertionError("expected the assertion to fail")


def test_renders_the_spec_example(unrewritten) -> None:
    """The underline spans exactly the failing sub-expression, per PEP 657's column info."""
    mod = unrewritten(
        """
        def check(resp, expected):
            assert resp.status_code == expected
        """
    )

    class Resp:
        status_code = 404

    assert _explain(mod.check, Resp(), 200) == (
        "    assert resp.status_code == expected\n           ~~~~~~~~~~~~~~~~~^^~~~~~~~~~"
    )


def test_bare_truthiness_assert(unrewritten) -> None:
    """No operator to single out, so the whole expression is underlined."""
    mod = unrewritten("def check(value):\n    assert value\n")
    assert _explain(mod.check, 0) == "    assert value\n           ^^^^^"


def test_membership_operator(unrewritten) -> None:
    mod = unrewritten("def check(a, b):\n    assert a in b\n")
    assert _explain(mod.check, "x", "abc") == "    assert a in b\n           ~~^^~~"


def test_not_in_operator(unrewritten) -> None:
    mod = unrewritten("def check(a, b):\n    assert a not in b\n")
    explanation = _explain(mod.check, "x", "xyz")
    assert explanation is not None
    assert "^^^^^^" in explanation


def test_operator_inside_a_call_is_not_mistaken_for_the_comparison(unrewritten) -> None:
    """Depth tracking: the `==` in a nested call must not steal the carets."""
    mod = unrewritten(
        """
        def eq(a, b):
            return a == b

        def check(x):
            assert eq(x, 1) == eq(x, x)
        """
    )
    explanation = _explain(mod.check, 3)
    assert explanation is not None
    caret_line = explanation.splitlines()[1]
    source_line = explanation.splitlines()[0]
    # The primary carets must sit on the top-level `==`, not the one inside `eq`.
    assert source_line[caret_line.index("^")] == "="
    assert source_line.index("==", source_line.index("eq(x, 1)")) == caret_line.index("^")


def test_operator_inside_a_string_literal_is_ignored(unrewritten) -> None:
    mod = unrewritten(
        """
        def check(x):
            assert x == "a == b"
        """
    )
    explanation = _explain(mod.check, "nope")
    assert explanation is not None
    caret_line = explanation.splitlines()[1]
    # Exactly one operator span, and it is the real one.
    assert caret_line.count("^") == 2


def test_operator_inside_a_triple_quoted_string_is_ignored(unrewritten) -> None:
    """A triple-quoted string is skipped as one unit, not read as an empty `''` plus a stray
    `'`."""
    mod = unrewritten("def check(x):\n    assert x == '''a == b'''\n")
    explanation = _explain(mod.check, "nope")
    assert explanation is not None
    caret_line = explanation.splitlines()[1]
    assert caret_line.count("^") == 2


def test_assert_prefixed_identifier_is_not_mistaken_for_the_keyword(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`assert_called_once()` (mock), `assertEqual(...)` (unittest), and
    `assert_frame_equal(...)` (pandas) all begin with the same six characters as the keyword but
    are identifiers, not statements."""

    def check() -> None:
        raise AssertionError()

    # `linecache.getline` is faked directly rather than raising through a real library, so this
    # pins the keyword check itself rather than any particular caller's plumbing.
    monkeypatch.setattr(linecache, "getline", lambda *a, **k: "    assert_called_once()\n")
    assert _explain(check) is None


def test_explicit_message_suppresses_the_floor(unrewritten) -> None:
    """`assert x, "why"` already said something more useful than a caret can."""
    mod = unrewritten('def check(x):\n    assert x, "x must be truthy"\n')
    assert _explain(mod.check, 0) is None


def test_multiline_assert_gets_no_carets(unrewritten) -> None:
    """A span crossing lines cannot be underlined honestly, so nothing is drawn."""
    mod = unrewritten(
        """
        def check(a, b):
            assert (
                a
                == b
            )
        """
    )
    assert _explain(mod.check, 1, 2) is None


def test_non_assert_failure_is_not_claimed(unrewritten) -> None:
    """An `AssertionError` raised by hand is not an `assert` statement."""
    mod = unrewritten("def check():\n    raise AssertionError()\n")
    assert _explain(mod.check) is None


def test_innermost_frame_wins(unrewritten) -> None:
    """The assert is in the deepest frame, not the caller that triggered it."""
    mod = unrewritten(
        """
        def inner(a, b):
            assert a == b

        def check(a, b):
            inner(a, b)
        """
    )
    explanation = _explain(mod.check, 1, 2)
    assert explanation is not None
    assert "assert a == b" in explanation


def test_source_at_reports_position(unrewritten) -> None:
    mod = unrewritten("def check(a, b):\n    assert a == b\n")
    try:
        mod.check(1, 2)
    except AssertionError as exc:
        tb = exc.__traceback__
        assert tb is not None
        while tb.tb_next is not None:
            tb = tb.tb_next
        source = source_at(tb)
        assert source is not None
        assert source.lineno == 2
        assert source.line == "assert a == b"
        assert source.filename.endswith("unrewritten_mod.py")


def test_missing_source_is_not_an_error() -> None:
    """A REPL, an `exec`'d string, or a file edited since import — all normal, all None."""
    namespace: dict[str, object] = {}
    exec(compile("def check():\n    assert False\n", "<generated>", "exec"), namespace)
    try:
        namespace["check"]()  # type: ignore[operator]
    except AssertionError as exc:
        assert explain_assertion(exc) is None
    else:
        raise AssertionError("expected failure")
