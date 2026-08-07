"""Extract _pytest/assertion/rewrite.py into a standalone module, recording
every edit required. Proves out the 'could this be a 1-2 file library' question."""

import re
import pathlib

SRC = pathlib.Path("/home/hubert/velox/pytest/src/_pytest/assertion/rewrite.py")
OUT = pathlib.Path(__file__).parent / "velox_rewrite.py"

src = SRC.read_text()
edits = []


def sub(old, new, label):
    global src
    assert old in src, f"NOT FOUND: {label}\n{old!r}"
    n = src.count(old)
    src = src.replace(old, new)
    edits.append((label, n, len(old.splitlines())))


# 1. saferepr: vendor it (155 LOC, zero pytest deps)
sub(
    "from _pytest._io.saferepr import DEFAULT_REPR_MAX_SIZE\n"
    "from _pytest._io.saferepr import saferepr\n"
    "from _pytest._io.saferepr import saferepr_unlimited\n"
    "from _pytest._version import version\n"
    "from _pytest.assertion import util\n"
    "from _pytest.config import Config\n"
    "from _pytest.fixtures import FixtureFunctionDefinition\n"
    "from _pytest.main import Session\n"
    "from _pytest.pathlib import absolutepath\n"
    "from _pytest.pathlib import fnmatch_ex\n"
    "from _pytest.stash import StashKey\n",
    "from _velox_shim import DEFAULT_REPR_MAX_SIZE\n"
    "from _velox_shim import saferepr\n"
    "from _velox_shim import saferepr_unlimited\n"
    "from _velox_shim import version\n"
    "from _velox_shim import util\n"
    "from _velox_shim import Config\n"
    "from _velox_shim import FixtureFunctionDefinition\n"
    "from _velox_shim import Session\n"
    "from _velox_shim import absolutepath\n"
    "from _velox_shim import fnmatch_ex\n"
    "from _velox_shim import StashKey\n",
    "import block: 11 pytest imports -> shim",
)

sub(
    "from _pytest.assertion.util import format_explanation as _format_explanation  # noqa:F401, isort:skip",
    "from _velox_shim import format_explanation as _format_explanation  # noqa:F401",
    "format_explanation import",
)

sub(
    "if TYPE_CHECKING:\n    from _pytest.assertion import AssertionState",
    "if TYPE_CHECKING:\n    from _velox_shim import AssertionState",
    "TYPE_CHECKING import",
)

# The injected import name must point at the standalone module
sub('"_pytest.assertion.rewrite",', '"velox_rewrite",', "injected @pytest_ar module name")

sub(
    "        from _pytest.warning_types import PytestAssertRewriteWarning\n\n"
    "        self.config.issue_config_time_warning(\n"
    "            PytestAssertRewriteWarning(\n"
    '                f"Module already imported so cannot be rewritten; {name}"\n'
    "            ),\n"
    "            stacklevel=5,\n"
    "        )",
    "        import warnings\n\n"
    '        warnings.warn(f"Module already imported so cannot be rewritten; {name}")',
    "_warn_already_imported (config.issue_config_time_warning)",
)

sub(
    "            import warnings\n\n"
    "            from _pytest.warning_types import PytestAssertRewriteWarning\n\n",
    "            import warnings\n\n",
    "visit_Assert tuple warning import",
)
sub(
    "                PytestAssertRewriteWarning(\n"
    '                    "assertion is always true, perhaps remove parentheses?"\n'
    "                ),",
    '                UserWarning("assertion is always true, perhaps remove parentheses?"),',
    "visit_Assert tuple warning class",
)

OUT.write_text(src)
print(f"wrote {OUT} ({len(src.splitlines())} lines)")
print("\nEDITS REQUIRED IN rewrite.py:")
tot = 0
for label, n, lines in edits:
    print(f"  - {label}  (x{n}, {lines} src lines)")
    tot += lines
print(f"\ntotal source lines touched: {tot} / {len(src.splitlines())}")
