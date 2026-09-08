"""Tests reading `beta/`'s own `client` alongside a fixture from the root conftest."""


def test_client_and_a_root_fixture(client, connection):
    assert client == {"transport": "beta"}
    assert connection == "connection(engine(sqlite:///showcase))"
