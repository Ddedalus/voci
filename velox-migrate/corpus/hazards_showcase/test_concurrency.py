"""Tests that pass one at a time and stop meaning the same thing when they overlap.

Contributes one case per process-global surface — the environment, `monkeypatch`, warning filters,
logging levels, `sys.modules`, the working directory, the locale, seeded randomness, module-level
state and a fixed resource — plus the loop cases: a blocking call inside a coroutine, a nested
`asyncio.run`, and the same blocking call in a plain `def`, which costs nothing.
"""

import asyncio
import importlib
import locale
import logging
import os
import random
import socket
import sys
import time
import warnings
from unittest import mock

CACHE: dict[str, int] = {}
COUNTER = 0


def _make_server(port):
    return f"server on {port}"


def test_environment_write():
    os.environ["FEATURE_FLAG"] = "on"
    os.environ.update({"REGION": "eu"})
    assert os.environ["FEATURE_FLAG"] == "on"


def test_monkeypatched_attribute(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "fake")
    monkeypatch.chdir(os.getcwd())
    assert socket.gethostname() == "fake"


def test_warning_filters():
    warnings.simplefilter("error")
    assert warnings.filters


def test_logging_level():
    logging.getLogger("app").setLevel(logging.DEBUG)
    logging.basicConfig()
    assert logging.getLogger("app").level == logging.DEBUG


def test_module_surgery():
    sys.modules["fake_module"] = mock.Mock()
    importlib.reload(logging)
    assert "fake_module" in sys.modules


def test_working_directory():
    os.chdir(os.getcwd())
    assert os.getcwd()


def test_interpreter_settings():
    locale.setlocale(locale.LC_ALL, "C")
    sys.setrecursionlimit(2000)
    assert True


def test_seeded_randomness():
    random.seed(1234)
    assert random.random() >= 0


def test_module_level_state():
    global COUNTER
    COUNTER += 1
    CACHE["seen"] = COUNTER
    assert CACHE


def test_fixed_resource():
    address = "localhost:8000"
    workdir = "/tmp/suite-cache"
    assert _make_server(port=8080) and address and workdir


async def test_blocking_call_in_a_coroutine():
    time.sleep(0.01)
    assert True


async def test_nested_event_loop():
    asyncio.run(asyncio.sleep(0))


def test_its_own_event_loop():
    loop = asyncio.new_event_loop()
    loop.run_until_complete(asyncio.sleep(0))
    loop.close()


def test_blocking_call_in_a_plain_def():
    time.sleep(0)
    assert True
