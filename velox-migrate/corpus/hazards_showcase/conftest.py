"""Root fixtures and hooks for the suite the audit is measured against.

Contributes the hooks-have-nowhere-to-go case (`pytest_configure`,
`pytest_collection_modifyitems`, `pytest_addoption`), the scopes-with-no-counterpart case
(`class_scoped`, `package_scoped`), the process-global-state-in-a-fixture case (`patched_env`,
overridden under `deep/` so the two share a name), the request-escape-hatch cases (`node_name`,
`option`, `closed`, `sometimes_closed`, `computed`), the legacy-tmpdir case (`legacy_dir`), the
indirect-parametrization target (`backend`), and the base of the over-budget override chain
(`settings` through `layer_f`, overridden under `deep/`).
"""

import os

import pytest


def pytest_addoption(parser):
    parser.addoption("--dsn", action="store", default="sqlite://", help="which database to use.")


def pytest_configure(config):
    config.addinivalue_line("markers", "extra: registered from a hook rather than the ini file.")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "deep" in item.nodeid:
            item.add_marker(pytest.mark.slow)


@pytest.fixture(scope="class")
def class_scoped():
    return object()


@pytest.fixture(scope="package")
def package_scoped():
    return object()


@pytest.fixture(autouse=True)
def patched_env(monkeypatch):
    monkeypatch.setenv("SUITE_MODE", "test")
    yield
    os.environ.pop("LEFTOVER", None)


@pytest.fixture
def node_name(request):
    return request.node.name


@pytest.fixture
def option(request):
    return request.config.getoption("--dsn")


@pytest.fixture
def closed(request):
    handle = {"open": True}

    def close():
        handle["open"] = False

    request.addfinalizer(close)
    return handle


@pytest.fixture
def sometimes_closed(request, node_name):
    handle = {"open": True}
    if node_name.startswith("test_"):
        request.addfinalizer(lambda: handle.update(open=False))
    return handle


@pytest.fixture
def computed(request):
    return request.getfixturevalue("layer_" + "a")


@pytest.fixture
def literal(request):
    return request.getfixturevalue("settings")


@pytest.fixture
def legacy_dir(tmpdir):
    return tmpdir.join("legacy.txt")


@pytest.fixture
def backend(request):
    return request.param


@pytest.fixture(scope="session")
def settings():
    return {"dsn": "sqlite://"}


@pytest.fixture
def layer_a(settings):
    return {**settings, "layer": "a"}


@pytest.fixture
def layer_b(layer_a):
    return {**layer_a, "layer": "b"}


@pytest.fixture
def layer_c(layer_b):
    return {**layer_b, "layer": "c"}


@pytest.fixture
def layer_d(layer_c):
    return {**layer_c, "layer": "d"}


@pytest.fixture
def layer_e(layer_d):
    return {**layer_d, "layer": "e"}


@pytest.fixture
def layer_f(layer_e):
    return {**layer_e, "layer": "f"}
