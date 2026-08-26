"""Tests for what an async plugin wires into a suite behind its tests' backs.

anyio is the case the corpus has no suite for: it registers a parametrized `anyio_backend`
fixture, hangs a `usefixtures` mark for it on every test it marks, and doubles the case list when
trio is installed — none of which appears in a line of the suite's own source. The dumps here are
a checked-in corpus dump with that wiring grafted on, which is what the plugin does to a real one.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from velox_migrate import audit, model
from velox_migrate.audit import Audit

CORPUS = Path(__file__).resolve().parents[1] / "corpus"
DUMPS = CORPUS / "dumps"
SUITE = "fixtures_showcase"

BACKEND_KEY = "anyio_backend"

_BACKEND_DEF: dict[str, Any] = {
    "argname": "anyio_backend",
    "scope": "module",
    "params": ["'asyncio'", "'trio'"],
    "ids": None,
    "autouse": False,
    "visibility": "",
    "kind": "FixtureDef",
    "direct_param": False,
    "argnames": ["request"],
    "func": {
        "module": "anyio.pytest_plugin",
        "qualname": "anyio_backend",
        "file": "${site_packages}/anyio/pytest_plugin.py",
        "lineno": 264,
        "wrapped": False,
    },
}


_PINNED_KEY = "suite-anyio-backend"

_PINNED_DEF: dict[str, Any] = {
    "argname": "anyio_backend",
    "scope": "function",
    "params": None,
    "ids": None,
    "autouse": False,
    "visibility": "",
    "kind": "FixtureDef",
    "direct_param": False,
    "argnames": [],
    "func": {
        "module": "conftest",
        "qualname": "anyio_backend",
        "file": "conftest.py",
        "lineno": 1,
        "wrapped": False,
    },
}


def _dump() -> dict[str, Any]:
    text = (DUMPS / f"{SUITE}-pytest-9.1.json").read_text(encoding="utf-8")
    return json.loads(text)


def _with_anyio(*backends: str, options: bool = False, pinned: bool = False) -> dict[str, Any]:
    """The corpus dump as anyio leaves it: one case per backend, per test it marks.

    Only the tests that carry no callspec of their own are marked, so the parametrization the
    corpus suite already has stays readable beside the one grafted on here. `options` writes each
    parameter as anyio's `(name, options)` pair rather than as a bare name, and `pinned` adds the
    suite's own `anyio_backend` above the plugin's, which is what pinning the backend looks like.
    """
    dump = _dump()
    dump["plugins"]["distinfo"].append(
        {"plugin": "anyio.pytest_plugin", "dist": "anyio", "version": "4.13.0"}
    )
    dump["fixture_defs"][BACKEND_KEY] = _BACKEND_DEF
    if pinned:
        dump["fixture_defs"][_PINNED_KEY] = _PINNED_DEF

    items: list[dict[str, Any]] = []
    for item in dump["items"]:
        if item.get("callspec") is not None:
            items.append(item)
            continue
        for index, backend in enumerate(backends):
            case = copy.deepcopy(item)
            case["nodeid"] = f"{item['nodeid']}[{backend}]"
            case["own_markers"] = [
                {"name": "anyio", "args": [], "kwargs": {}},
                {"name": "usefixtures", "args": [f"'{BACKEND_KEY}'"], "kwargs": {}},
            ]
            case["markers_with_origin"] = [
                {"from": case["nodeid"], **mark} for mark in case["own_markers"]
            ]
            case["usefixtures"] = [BACKEND_KEY]
            case["initialnames"] = [BACKEND_KEY, *case["initialnames"]]
            case["names_closure"] = [BACKEND_KEY, *case["names_closure"]]
            chain = [BACKEND_KEY, _PINNED_KEY] if pinned else [BACKEND_KEY]
            case["name2fixturedefs"][BACKEND_KEY] = chain
            case["callspec"] = {
                "id": backend,
                "idlist": [backend],
                "params": {BACKEND_KEY: f"('{backend}', {{}})" if options else f"'{backend}'"},
                "indices": {BACKEND_KEY: index},
                "marks": [],
            }
            items.append(case)
    dump["items"] = items
    return dump


def _audit(dump: dict[str, Any]) -> Audit:
    return audit.run(model.build(dump), root=CORPUS / SUITE)


def _by_code(result: Audit, code: str) -> tuple:
    return tuple(finding for finding in result.findings if finding.code == code)


def test_the_cases_a_backend_parametrization_puts_on_another_loop_are_blocked() -> None:
    result = _audit(_with_anyio("asyncio", "trio"))

    findings = _by_code(result, "VX324")
    assert findings
    blocked = {nodeid for finding in findings for nodeid in finding.tests}
    every_trio_case = {
        item.nodeid
        for item in model.build(_with_anyio("asyncio", "trio")).items
        if item.nodeid.endswith("[trio]")
    }
    assert blocked == every_trio_case


def test_a_backend_parametrization_over_asyncio_alone_is_nothing_to_report() -> None:
    result = _audit(_with_anyio("asyncio"))

    assert _by_code(result, "VX324") == ()


def test_a_backend_named_in_a_pair_with_its_options_is_read_the_same_way() -> None:
    # anyio takes either the backend's name or a `(name, options)` pair, and a suite that passes
    # uvloop options to asyncio parametrizes over the pair form for every backend it lists.
    result = _audit(_with_anyio("asyncio", "trio", options=True))

    findings = _by_code(result, "VX324")
    assert findings
    assert all(finding.detail["backend"] == "trio" for finding in findings)


def test_pinning_the_backend_in_the_suite_does_not_hand_its_marks_back() -> None:
    # The prefactor for this is a suite-level `anyio_backend`, which wins the name without
    # stopping anyio from hanging the mark — so the mark is still the plugin's wiring.
    result = _audit(_with_anyio("asyncio", pinned=True))

    named = {
        finding.detail.get("fixture")
        for code in ("VX009", "VX010")
        for finding in _by_code(result, code)
    }
    assert BACKEND_KEY not in named


def test_the_usefixtures_mark_the_plugin_hangs_on_a_test_is_not_the_suites() -> None:
    # The suite writes no `usefixtures` of its own, so every one of these marks is anyio's, and
    # migration deletes anyio rather than writing a `velox.use(...)` for what it wired.
    result = _audit(_with_anyio("asyncio"))

    named = {
        finding.detail.get("fixture")
        for code in ("VX009", "VX010")
        for finding in _by_code(result, code)
    }
    assert BACKEND_KEY not in named


def test_a_fixture_the_runner_replaces_is_not_reported_as_one_with_no_velox_path() -> None:
    # anyio's whole job is running the test, which velox does itself, so `anyio_backend` is not a
    # dependency anybody has to write into the suite.
    result = _audit(_with_anyio("asyncio"))

    assert BACKEND_KEY not in {
        finding.detail.get("fixture") for finding in _by_code(result, "VX030")
    }
