"""Tests under the leaf override, which name the overriding definition and nothing copied."""


def test_token(token):
    assert token == "token:leaf"


def test_engine_is_the_root_one(engine):
    assert engine == "engine:sqlite://root"
