"""Soundness cases for `voci._affected.resolve`: the name closure, effect folding, string index
and whole-module fallback (`plans/affected-tests-plan.md`, "What a passing test depends on").
Mirrors the cases from `research/affected/reports/name-deps.md`'s probe, adapted to voci's own
`(path, name)`/`(path, qualname)` keys rather than the prototype's positional `BlockKey`s -- see
`resolve.py`'s own module docstring for where the two designs diverge (a `World.closure` on
static references alone; `World.effect_fold_target` as a separate query, not folded into the
same BFS).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from voci._affected.blocks import parse_blocks
from voci._affected.resolve import DefKey, ModuleKey, NameKey, World


def _world(files: Mapping[str, str]) -> tuple[World, dict[str, Path]]:
    """Builds a `World` straight from `{dotted name: source}` -- no real files needed, since
    `World` never touches disk once given explicit dotted names."""
    paths = {dotted: Path(f"/proj/{dotted.replace('.', '/')}.py") for dotted in files}
    return World({paths[dotted]: (dotted, source) for dotted, source in files.items()}), paths


# Constants, aliases and transitivity
# ------------------------------------------------------------------------


def test_a_constant_imported_by_name_is_in_the_closure() -> None:
    world, paths = _world(
        {
            "config": "TIMEOUT = 5\n",
            "app": "from config import TIMEOUT\ndef handler():\n    return TIMEOUT\n",
        }
    )
    closure = world.closure([DefKey(paths["app"], "handler")])
    assert NameKey(paths["config"], "TIMEOUT") in closure


def test_a_constant_derived_from_another_constant_is_transitive() -> None:
    world, paths = _world(
        {
            "config": "RETRIES = 3\nTOTAL_BUDGET = RETRIES * 2\n",
            "app": "from config import TOTAL_BUDGET\ndef handler():\n    return TOTAL_BUDGET\n",
        }
    )
    closure = world.closure([DefKey(paths["app"], "handler")])
    assert NameKey(paths["config"], "TOTAL_BUDGET") in closure
    assert NameKey(paths["config"], "RETRIES") in closure


def test_an_aliased_import_resolves_to_the_real_binding() -> None:
    world, paths = _world(
        {
            "config": "TIMEOUT = 5\n",
            "app": "from config import TIMEOUT as T\ndef handler():\n    return T\n",
        }
    )
    closure = world.closure([DefKey(paths["app"], "handler")])
    assert NameKey(paths["config"], "TIMEOUT") in closure


def test_a_reexport_hub_only_pulls_in_the_name_actually_used() -> None:
    """The point of name-level (not file-level) resolution: an unrelated statement in the same
    file as the one actually referenced is correctly excluded."""
    world, paths = _world(
        {
            "routing": "class Router:\n    pass\nUNRELATED = 1\n",
            "app": "from routing import Router\ndef handler():\n    return Router()\n",
        }
    )
    closure = world.closure([DefKey(paths["app"], "handler")])
    assert NameKey(paths["routing"], "Router") in closure
    assert NameKey(paths["routing"], "UNRELATED") not in closure


def test_a_brand_new_undecorated_function_is_reached_by_nothing() -> None:
    world, paths = _world({"app": "def old():\n    return 1\ndef new():\n    return 2\n"})
    closure = world.closure([DefKey(paths["app"], "old")])
    assert NameKey(paths["app"], "new") not in closure
    assert DefKey(paths["app"], "new") not in closure


# Classes: bases, fields, methods
# ------------------------------------------------------------------------


def test_referencing_a_class_pulls_in_its_bases_and_fields() -> None:
    world, paths = _world(
        {
            "models": "class Base:\n    pass\nclass Model(Base):\n    x: int = 1\n",
            "app": "from models import Model\ndef handler():\n    return Model()\n",
        }
    )
    closure = world.closure([DefKey(paths["app"], "handler")])
    assert NameKey(paths["models"], "Model") in closure
    assert NameKey(paths["models"], "Base") in closure


def test_a_method_that_ran_pulls_in_its_top_level_class_block() -> None:
    world, paths = _world({"app": "class C(Base):\n    def m(self):\n        return 1\n"})
    closure = world.closure([DefKey(paths["app"], "C.m")])
    assert NameKey(paths["app"], "C") in closure


# Effect folding: decorators, registries, cross-file routes
# ------------------------------------------------------------------------


def test_a_decorated_route_folds_onto_the_app_it_references() -> None:
    routes_source = "from main import app\n@app.get('/a')\ndef foo():\n    return 1\n"
    world, paths = _world(
        {
            "main": "from fastapi import FastAPI\napp = FastAPI()\n",
            "routes": routes_source,
        }
    )
    routes_file_blocks = parse_blocks(routes_source, str(paths["routes"]))
    statement = next(b for b in routes_file_blocks if b.qualname is None and "foo" in b.binds)
    assert world.effect_fold_target(paths["routes"], statement) == frozenset(
        {NameKey(paths["main"], "app")}
    )


def test_a_registry_decorator_factory_folds_onto_the_registry_through_one_hop() -> None:
    handlers_source = (
        "from registry import register\n@register('key')\ndef handler():\n    return 1\n"
    )
    world, paths = _world(
        {
            "registry": (
                "REGISTRY = {}\n"
                "def register(key):\n"
                "    def decorator(fn):\n"
                "        REGISTRY[key] = fn\n"
                "        return fn\n"
                "    return decorator\n"
            ),
            "handlers": handlers_source,
        }
    )
    handlers_blocks = parse_blocks(handlers_source, str(paths["handlers"]))
    statement = next(b for b in handlers_blocks if b.qualname is None and "handler" in b.binds)
    targets = world.effect_fold_target(paths["handlers"], statement)
    # Widened onto REGISTRY through the one-hop factory look-through; "register" itself is also
    # a legitimate, separate fold target, since the decorator references it directly too.
    assert NameKey(paths["registry"], "REGISTRY") in targets


def test_a_session_global_effect_has_no_fold_target() -> None:
    """`logging.basicConfig()` mutates process-wide state, not a first-party name -- it's
    reached by touching its own module (`closure`'s `session_global`), not by folding onto
    anything."""
    setup_source = "import logging\nlogging.basicConfig()\n"
    world, paths = _world({"setup": setup_source})
    blocks = parse_blocks(setup_source, str(paths["setup"]))
    statement = next(b for b in blocks if b.effect)
    assert world.effect_fold_target(paths["setup"], statement) == frozenset()


def test_a_session_global_effect_is_pulled_in_by_touching_its_own_module() -> None:
    world, paths = _world(
        {
            "setup": "import logging\nlogging.basicConfig()\nLEVEL = 1\n",
            "app": "from setup import LEVEL\ndef handler():\n    return LEVEL\n",
        }
    )
    closure = world.closure([DefKey(paths["app"], "handler")])
    # The bare `logging.basicConfig()` expression statement binds nothing, so it has no name of
    # its own to check for in a NameKey -- but the *module's* own import (`logging`) is
    # module-global and is pulled in once `setup` is touched.
    assert NameKey(paths["setup"], "logging") in closure


# Whole-module fallback
# ------------------------------------------------------------------------


def test_a_whole_module_import_alias_pulls_in_every_top_level_name() -> None:
    world, paths = _world(
        {
            "httpx_like": "def get():\n    return 1\nVERSION = '1.0'\n",
            "app": "import httpx_like\ndef handler():\n    return httpx_like.get()\n",
        }
    )
    closure = world.closure([DefKey(paths["app"], "handler")])
    assert NameKey(paths["httpx_like"], "get") in closure
    assert NameKey(paths["httpx_like"], "VERSION") in closure


def test_module_getattr_triggers_whole_module_fallback() -> None:
    world, paths = _world(
        {
            "lazy": "X = 1\ndef __getattr__(name):\n    return X\n",
            "app": "from lazy import X\ndef handler():\n    return X\n",
        }
    )
    closure = world.closure([DefKey(paths["app"], "handler")])
    assert NameKey(paths["lazy"], "X") in closure
    # PEP 562 modules may return anything from anywhere in the module for any name, so the whole
    # module -- not just X -- is depended on.
    assert DefKey(paths["lazy"], "__getattr__") in closure


def test_a_third_party_import_resolves_to_a_module_key() -> None:
    world, paths = _world(
        {"app": "import requests\ndef handler():\n    return requests.get('x')\n"}
    )
    closure = world.closure([DefKey(paths["app"], "handler")])
    assert ModuleKey("requests") in closure


# Strings: forward refs, registries
# ------------------------------------------------------------------------


def test_a_bare_identifier_string_forward_ref_resolves_like_a_name() -> None:
    world, paths = _world(
        {
            "models": "class Manager:\n    pass\n",
            "app": ("from models import Manager\nclass Employee:\n    manager: 'Manager'\n"),
        }
    )
    closure = world.closure([NameKey(paths["app"], "Employee")])
    assert NameKey(paths["models"], "Manager") in closure


def test_a_string_in_a_class_body_is_indexed() -> None:
    world, paths = _world(
        {
            "models": "class Address:\n    __tablename__ = 'addresses'\n",
            "app": "RELATION = 'Address'\n",
        }
    )
    closure = world.closure([NameKey(paths["app"], "RELATION")])
    assert NameKey(paths["models"], "Address") in closure


# TYPE_CHECKING / try-except-ImportError -- ordinary conditional statements
# ------------------------------------------------------------------------


def test_a_try_except_importerror_fallback_def_is_reached_like_any_other() -> None:
    world, paths = _world(
        {
            "app": (
                "try:\n"
                "    from ujson import dumps\n"
                "except ImportError:\n"
                "    def dumps(x):\n"
                "        return str(x)\n"
                "def handler():\n"
                "    return dumps(1)\n"
            )
        }
    )
    closure = world.closure([DefKey(paths["app"], "handler")])
    assert NameKey(paths["app"], "dumps") in closure


# Star imports
# ------------------------------------------------------------------------


def test_star_import_resolves_precisely_through_dunder_all() -> None:
    world, paths = _world(
        {
            "pkg": "__all__ = ['Public']\nclass Public:\n    pass\nclass Private:\n    pass\n",
            "app": "from pkg import *\ndef handler():\n    return Public()\n",
        }
    )
    closure = world.closure([DefKey(paths["app"], "handler")])
    assert NameKey(paths["pkg"], "Public") in closure
    assert NameKey(paths["pkg"], "Private") not in closure


# Relative imports
# ------------------------------------------------------------------------


def test_a_relative_import_resolves_within_the_package() -> None:
    world, paths = _world(
        {
            "pkg": "",
            "pkg.config": "TIMEOUT = 5\n",
            "pkg.app": "from .config import TIMEOUT\ndef handler():\n    return TIMEOUT\n",
        }
    )
    closure = world.closure([DefKey(paths["pkg.app"], "handler")])
    assert NameKey(paths["pkg.config"], "TIMEOUT") in closure


# A nested import is a reference of its own def block
# ------------------------------------------------------------------------


def test_a_nested_import_inside_a_function_resolves_to_its_own_module() -> None:
    world, paths = _world(
        {
            "helpers": "X = 1\n",
            "app": "def handler():\n    import helpers\n    return helpers.X\n",
        }
    )
    closure = world.closure([DefKey(paths["app"], "handler")])
    assert NameKey(paths["helpers"], "X") in closure


def test_a_nested_import_shadows_a_same_named_top_level_binding() -> None:
    """`shared` means two different things depending on scope: a plain module-level name in
    `app` itself when read at module level, and `inner`'s import alias inside `handler` -- the
    nested import must resolve to the latter when `handler`'s own body is what's asking."""
    world, paths = _world(
        {
            "inner": "X = 2\n",
            "app": (
                "shared = object()\n"
                "def handler():\n"
                "    import inner as shared\n"
                "    return shared.X\n"
            ),
        }
    )
    closure = world.closure([DefKey(paths["app"], "handler")])
    assert NameKey(paths["inner"], "X") in closure
    assert NameKey(paths["app"], "shared") not in closure


# Every statement (and def) binding a name matters, not just the first
# ------------------------------------------------------------------------


def test_an_if_else_reassignment_pulls_in_both_branches_own_references() -> None:
    world, paths = _world(
        {
            "flag_a": "A = 1\n",
            "flag_b": "B = 1\n",
            "config": (
                "from flag_a import A\n"
                "from flag_b import B\n"
                "if A:\n"
                "    DEBUG = A\n"
                "else:\n"
                "    DEBUG = B\n"
            ),
            "app": "from config import DEBUG\ndef handler():\n    return DEBUG\n",
        }
    )
    closure = world.closure([DefKey(paths["app"], "handler")])
    assert NameKey(paths["flag_a"], "A") in closure
    assert NameKey(paths["flag_b"], "B") in closure


def test_an_if_else_def_pulls_in_both_branches_own_references() -> None:
    world, paths = _world(
        {
            "flag_a": "A = 1\n",
            "flag_b": "B = 1\n",
            "app": (
                "from flag_a import A\n"
                "from flag_b import B\n"
                "if True:\n"
                "    def dup():\n"
                "        return A\n"
                "else:\n"
                "    def dup():\n"
                "        return B\n"
                "def handler():\n"
                "    return dup()\n"
            ),
        }
    )
    closure = world.closure([DefKey(paths["app"], "handler")])
    assert NameKey(paths["flag_a"], "A") in closure
    assert NameKey(paths["flag_b"], "B") in closure
