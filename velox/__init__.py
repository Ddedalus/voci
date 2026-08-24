"""velox: a fast, concurrent test runner for fully-async Python codebases.

This is the whole public surface — everything else is private and may move. It exports
dependency injection (`fixture`, `Depends`, `Scope`, `use`), marks for selecting and shaping tests
(`skip`, `xfail`, `parametrize`, `tag`, ...), runtime skip/fail signals (`Skipped`, `Failed`),
built-in fixtures (`tmp_path`, `capture`, `log_records`, `test_info`), and assertion helpers
(`raises`, `approx`).
"""

from velox._assertions.approx import Approx, approx
from velox._assertions.raises import ExceptionInfo, raises
from velox._builtins.fixtures import (
    Capture,
    LegacyPath,
    LegacyTmpPathFactory,
    LogRecords,
    TestInfo,
    TmpPathFactory,
    capture,
    log_records,
    test_info,
    tmp_path,
    tmp_path_factory,
    tmpdir,
    tmpdir_factory,
)
from velox._collection.requires import use
from velox._di.fixtures import Depends, Fixture, Scope, fixture
from velox._marks import (
    MarkDecorator,
    ParamCase,
    case,
    isolated,
    parametrize,
    skip,
    skipif,
    solo,
    tag,
    timeout,
    xfail,
)
from velox._outcomes import Failed, Skipped
from velox._version import __version__

# Grouped by purpose above (DI, marks, built-in fixtures, assertions) but sorted alphabetically
# here. Everything capitalized is a type you may need to name in an annotation.
__all__ = [
    "Approx",
    "Capture",
    "Depends",
    "ExceptionInfo",
    "Failed",
    "Fixture",
    "LegacyPath",
    "LegacyTmpPathFactory",
    "LogRecords",
    "MarkDecorator",
    "ParamCase",
    "Scope",
    "Skipped",
    "TestInfo",
    "TmpPathFactory",
    "__version__",
    "approx",
    "capture",
    "case",
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
    "tmpdir",
    "tmpdir_factory",
    "use",
    "xfail",
]
