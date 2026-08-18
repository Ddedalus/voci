"""Root tests: the definitions no override reaches, which keep the objects written for them."""


def test_engine(engine):
    assert engine == "engine:sqlite://root"


def test_client(client):
    assert client == {"engine": "engine:sqlite://root", "stamp": "stamp:showcase"}


def test_token(token):
    assert token == "token:root"
