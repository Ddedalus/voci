"""velox: a fast, concurrent test runner for fully-async Python codebases.

This is the whole public surface — everything else is private and may move. It exports
dependency injection (`fixture`, `Depends`, `Scope`), marks for selecting and shaping tests
(`skip`, `xfail`, `parametrize`, `tag`, ...), built-in fixtures (`tmp_path`, `capture`,
`log_records`, `test_info`), and assertion helpers (`raises`, `approx`).
"""

from velox._approx import Approx, approx
from velox._builtins.fixtures import (
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
from velox._di.fixtures import Depends, Fixture, Injection, Scope, fixture
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
from velox._raises import ExceptionInfo, RaisesContext, raises
from velox._version import __version__

# Grouped by purpose above (DI, marks, built-in fixtures, assertions) but sorted alphabetically
# here. Everything capitalized is a type you may need to name in an annotation.
__all__ = [
    "Approx",
    "Capture",
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
