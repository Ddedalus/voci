"""Minimal replacements for everything rewrite.py imports from pytest."""

from __future__ import annotations

import fnmatch
import os
import re
import sys
from pathlib import Path, PurePath

# --- vendored verbatim from _pytest/_io/saferepr.py (155 LOC, stdlib only) ---
sys.path.insert(0, "/home/hubert/velox/pytest/src")
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location(
    "_velox_saferepr", "/home/hubert/velox/pytest/src/_pytest/_io/saferepr.py"
)
_sr = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_sr)
saferepr = _sr.saferepr
saferepr_unlimited = _sr.saferepr_unlimited
DEFAULT_REPR_MAX_SIZE = _sr.DEFAULT_REPR_MAX_SIZE

version = "velox-0.1"


# --- from _pytest/pathlib.py ---
def absolutepath(path):
    return Path(os.path.abspath(path))


sep = os.sep
posix_sep = "/"


def fnmatch_ex(pattern: str, path) -> bool:
    path = PurePath(path)
    iswin32 = sys.platform.startswith("win")
    if iswin32 and sep not in pattern and posix_sep in pattern:
        pattern = pattern.replace(posix_sep, sep)
    if sep not in pattern:
        name = path.name
    else:
        name = str(path)
        if path.is_absolute() and not os.path.isabs(pattern):
            pattern = f"*{os.sep}{pattern}"
    return fnmatch.fnmatch(name, pattern)


# --- from _pytest/stash.py ---
class StashKey:
    def __class_getitem__(cls, item):
        return cls


# --- config / session / fixtures stand-ins ---
class Config:
    VERBOSITY_ASSERTIONS = "assertions"

    def __init__(self, ini=None, verbosity=0):
        self._ini = ini or {}
        self._verbosity = verbosity
        self.stash = {}

    def getini(self, name):
        return self._ini[name]

    def get_verbosity(self, kind=None):
        return self._verbosity


class Session:
    _initialpaths: frozenset = frozenset()

    def isinitpath(self, p):
        return False


class FixtureFunctionDefinition:
    pass


class AssertionState:
    def __init__(self, config=None, mode="rewrite"):
        self.mode = mode
        self.hook = None

    def trace(self, msg):
        pass


# --- from _pytest/assertion/util.py: only the 3 globals + format_explanation ---
class _Util:
    _reprcompare = None
    _assertion_pass = None
    _config = None


util = _Util()


def _split_explanation(explanation: str) -> list[str]:
    raw_lines = (explanation or "").split("\n")
    lines = [raw_lines[0]]
    for values in raw_lines[1:]:
        if values and values[0] in ["{", "}", "~", ">"]:
            lines.append(values)
        else:
            lines[-1] += "\\n" + values
    return lines


def _format_lines(lines):
    result = list(lines[:1])
    stack = [0]
    stackcnt = [0]
    for line in lines[1:]:
        if line.startswith("{"):
            s = "and   " if stackcnt[-1] else "where "
            stack.append(len(result))
            stackcnt[-1] += 1
            stackcnt.append(0)
            result.append(" +" + "  " * (len(stack) - 1) + s + line[1:])
        elif line.startswith("}"):
            stack.pop()
            stackcnt.pop()
            result[stack[-1]] += line[1:]
        else:
            stack[-1] += 1
            indent = len(stack) if line.startswith("~") else len(stack) - 1
            result.append("  " * indent + line[1:])
    return result


def format_explanation(explanation: str) -> str:
    return "\n".join(_format_lines(_split_explanation(explanation)))
