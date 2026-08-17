"""Tests one directory down, reading that directory's fixtures and the root's together.

Contributes the cross-directory import case and the aliased-import case: this module binds
`payload` as a helper of its own, so the fixture of that name cannot be imported under it.
"""


def payload():
    """A helper that happens to be named like the fixture next door."""
    return {"kind": "invoice", "qty": 1}


def test_fixture_and_helper_share_a_name(payload):
    assert payload == {"kind": "order", "qty": 2}


def test_route_and_a_root_fixture(route, dsn):
    assert route == "/v1/orders"
    assert dsn == "sqlite:///showcase"
