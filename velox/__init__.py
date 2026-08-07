"""velox: a fast, concurrent test runner for fully-async Python codebases.

This is the whole public surface. Everything else is private and may move.

Currently the *declarative* half is real — fixtures build their dependency plan, marks attach
their records, `raises` and `approx` work. The execution half (collection, scheduling, running,
reporting) is not implemented yet; see `spec/`.
"""

from velox._assertions import Approx, ExceptionInfo, RaisesContext, approx, raises
from velox._builtins import (
    Capture,
    LogRecords,
    TestInfo,
    TmpPathFactory,
    capture,
    log_records,
    test_info,
    tmp_path,
    tmp_path_factory,
)
from velox._fixtures import Constant, Depends, Fixture, Injection, Scope, fixture
from velox._marks import (
    Marks,
    ParamSet,
    Skip,
    SkipIf,
    XFail,
    isolated,
    parametrize,
    skip,
    skipif,
    solo,
    tag,
    timeout,
    xfail,
)

__version__ = "0.1.0"

# Grouped by purpose in the imports above; sorted here because that is what the linter wants.
# DI: Depends, Fixture, Scope, fixture. Marks: isolated, parametrize, skip, skipif, solo, tag,
# timeout, xfail. Built-in fixtures: capture, log_records, test_info, tmp_path,
# tmp_path_factory. Assertions: approx, raises. Everything capitalized is a type you may need to
# name in an annotation.
__all__ = [
    "Approx",
    "Capture",
    "Constant",
    "Depends",
    "ExceptionInfo",
    "Fixture",
    "Injection",
    "LogRecords",
    "Marks",
    "ParamSet",
    "RaisesContext",
    "Scope",
    "Skip",
    "SkipIf",
    "TestInfo",
    "TmpPathFactory",
    "XFail",
    "__version__",
    "approx",
    "capture",
    "fixture",
    "isolated",
    "log_records",
    "parametrize",
    "raises",
    "skip",
    "skipif",
    "solo",
    "tag",
    "test_info",
    "timeout",
    "tmp_path",
    "tmp_path_factory",
    "xfail",
]
