"""Tests-as-functions used for the soundness table. Each is a def block; the
harness script extracts its body's references and computes the closure
against the pkg/ analyzer, then checks whether an edited statement's
BlockKey ended up in that closure."""
import pkg
from pkg import config
from pkg.app_mod import app, handle_user
from pkg.config import TIMEOUT
from pkg.consumers import uses_from_import, uses_module_attr
from pkg.dataclasses_mod import make_widget
from pkg.loader import load_plugin
from pkg.models import make_default_user
from pkg.registry import call_registered
from pkg.typecheck_mod import public_fn


def test_const_via_from_import():
    assert uses_from_import() == TIMEOUT + 1


def test_const_via_module_attr():
    assert uses_module_attr() == config.TOTAL_BUDGET


def test_pydantic_model():
    user = make_default_user()
    assert user.address.city == "x"


def test_registry_greet():
    assert call_registered("greet") == "hello"


def test_reexport_hub_router():
    r = pkg.Router()
    assert r.prefix == "/"


def test_app_handler_param_model():
    user = make_default_user()
    assert handle_user(user) == 1


def test_dataclass_widget():
    w = make_widget()
    assert w.name == "widget"


def test_typecheck_module():
    assert public_fn() is not None


def test_dynamic_plugin_load():
    assert load_plugin("foo") == "foo-plugin"


def test_extra_route_registered():
    assert "/extra" in app.routes
