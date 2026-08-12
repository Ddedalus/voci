"""Tests for `velox._report.color`: the enabled/disabled decision and `paint`'s wrapping."""

from __future__ import annotations

import io

import pytest
from velox._report.color import GREEN, color_enabled, paint


class _FakeTTYStream(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_color_enabled_requires_a_real_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert color_enabled(io.StringIO()) is False
    assert color_enabled(_FakeTTYStream()) is True


def test_no_color_disables_even_a_real_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """https://no-color.org: any value, including an empty string, disables color."""
    monkeypatch.setenv("NO_COLOR", "")
    assert color_enabled(_FakeTTYStream()) is False


def test_paint_wraps_only_when_enabled() -> None:
    assert paint("ok", GREEN, enabled=True) == f"{GREEN}ok\x1b[0m"
    assert paint("ok", GREEN, enabled=False) == "ok"
