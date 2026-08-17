"""Tests reading `alpha/`'s own `client`, and the package-scoped fixture from the root."""


def test_client_and_package_scope(client, package_scoped):
    assert client == {"transport": "alpha"}
    assert package_scoped == {"shared": "package"}
