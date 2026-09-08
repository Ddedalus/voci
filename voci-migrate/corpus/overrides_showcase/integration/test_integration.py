"""Tests directly under the override, reading every rung of the specialized chain."""

import pytest


def test_settings(settings):
    assert settings["dsn"] == "sqlite://integration"


@pytest.mark.chain
def test_engine(engine):
    assert engine == "engine:sqlite://integration"


def test_client(client):
    assert client == {"engine": "engine:sqlite://integration", "stamp": "stamp:showcase"}


def test_report(report):
    assert report == "report:engine:sqlite://integration"
