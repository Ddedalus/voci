"""Guards on the vendored tree: no hand-edits, the pytest submodule hasn't moved, and no
runtime dependency on pytest.

`--check` needs the pytest submodule, so the regeneration test skips without it; the rest run
anywhere, including from an installed wheel.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

VENDOR_DIR = Path(__file__).resolve().parents[2] / "voci" / "_assertions" / "_vendor"
REPO = Path(__file__).resolve().parents[2]
#: The generated files only. `__init__.py` and `_shim.py` are voci's own, hand-written.
VENDORED_SOURCES = sorted(
    p for p in VENDOR_DIR.glob("*.py") if p.name not in {"__init__.py", "_shim.py"}
)


def test_there_is_something_vendored() -> None:
    assert VENDORED_SOURCES, f"no vendored sources under {VENDOR_DIR}"


@pytest.mark.parametrize("path", VENDORED_SOURCES, ids=lambda p: p.name)
def test_no_runtime_dependency_on_pytest(path: Path) -> None:
    """voci must not import pytest at run time. Comments are exempt — the generated header
    names the upstream file it came from."""
    offenders = [
        f"{i}: {line.strip()}"
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if "_pytest" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize("path", VENDORED_SOURCES, ids=lambda p: p.name)
def test_generated_header_is_intact(path: Path) -> None:
    """`# ruff: noqa` keeps the files byte-identical to upstream, and the header is what
    tells a reader not to edit them by hand."""
    head = path.read_text(encoding="utf-8").split("\n", 6)
    assert head[0] == "# ruff: noqa"
    assert "Vendored from pytest" in head[2]
    assert "pytest commit:" in head[4]


def test_pytest_is_not_imported_by_using_the_rewriter() -> None:
    """A fresh interpreter that installs and uses voci's assertion machinery must not end up
    with `_pytest` in `sys.modules`."""
    code = (
        "import sys, tempfile, pathlib\n"
        "from voci._assertions.rewrite import install, uninstall, assertion_context, Config\n"
        "d = pathlib.Path(tempfile.mkdtemp())\n"
        "(d / 'test_x.py').write_text('def f():\\n    assert [1] == [2]\\n')\n"
        "install([d], cache_dir=d / 'cache')\n"
        "sys.path.insert(0, str(d))\n"
        "import test_x\n"
        "with assertion_context(Config()):\n"
        "    try:\n"
        "        test_x.f()\n"
        "    except AssertionError as e:\n"
        "        assert 'At index 0 diff' in str(e), str(e)\n"
        "uninstall()\n"
        "leaked = [m for m in sys.modules if m == '_pytest' or m.startswith('_pytest.')]\n"
        "print('LEAKED:', leaked)\n"
        "raise SystemExit(1 if leaked else 0)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_vendored_tree_matches_the_script() -> None:
    """Regenerating the vendored tree from the pytest submodule must be a no-op."""
    if not (REPO / "oss" / "pytest" / "src" / "_pytest").is_dir():
        pytest.skip("pytest submodule not checked out")
    result = subprocess.run(
        [sys.executable, "scripts/vendor_assertion.py", "--check"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_shim_is_hand_written_not_generated() -> None:
    """`_shim.py` is the one file in the tree that is voci's own; it must not grow a
    generated header, because regenerating would blow it away."""
    assert "# ruff: noqa" not in (VENDOR_DIR / "_shim.py").read_text().split("\n")[0]


def test_vendor_md_exists_and_records_the_commit() -> None:
    vendor_md = (VENDOR_DIR / ".." / "VENDOR.md").resolve()
    text = vendor_md.read_text()
    assert "Upstream:" in text
    assert "Applied edits" in text
