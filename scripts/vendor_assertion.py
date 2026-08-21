#!/usr/bin/env python3
"""Re-vendor pytest's assertion subsystem into `velox/_assertions/_vendor/`.

Run this, not a manual copy. Re-vendoring is a deliberate act (spec/07 §1), and the whole
argument for vendoring rather than reimplementing is that the diff stays small and mechanical:
this script *is* the coupling-point list, and it fails loudly when an edit no longer applies.

    uv run python scripts/vendor_assertion.py

Reads `oss/pytest/` (the submodule), writes the vendored tree plus
`velox/_assertions/_vendor/VENDOR.md`.
Vendored files are kept byte-identical to upstream apart from the edits recorded here
(spec/07 Q17), so `diff` against a fresh pytest checkout stays readable.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTEST_SRC = REPO / "oss" / "pytest" / "src" / "_pytest"
DST = REPO / "velox" / "_assertions" / "_vendor"
VENDOR_MD = REPO / "velox" / "_assertions" / "VENDOR.md"

VENDOR_PKG = "velox._assertions._vendor"

# Upstream file -> vendored module name. `_pytest/assertion/__init__.py` is deliberately absent:
# it is pytest's plugin glue (hooks, config wiring), which velox replaces with
# `velox/_assertions/rewrite.py`.
FILES = {
    "assertion/rewrite.py": "rewrite.py",
    "assertion/util.py": "util.py",
    "assertion/truncate.py": "truncate.py",
    "assertion/compare_text.py": "compare_text.py",
    "assertion/highlight.py": "highlight.py",
    "assertion/_typing.py": "_typing.py",
    "assertion/_guards.py": "_guards.py",
    "assertion/_compare_any.py": "_compare_any.py",
    "assertion/_compare_mapping.py": "_compare_mapping.py",
    "assertion/_compare_sequence.py": "_compare_sequence.py",
    "assertion/_compare_set.py": "_compare_set.py",
    "_io/saferepr.py": "saferepr.py",
    "_io/pprint.py": "_pprint.py",
}

# Every `_pytest.*` module the vendored files import, and where it resolves to here. Modules
# mapped to `_shim` are the ones velox reimplements in ~130 LOC rather than vendoring.
MODULE_MAP = {
    "_pytest._io.saferepr": f"{VENDOR_PKG}.saferepr",
    "_pytest._io.pprint": f"{VENDOR_PKG}._pprint",
    "_pytest.assertion": VENDOR_PKG,
    # `from _pytest import outcomes` — the bare package form, so the name imported must be
    # something the shim exposes.
    "_pytest": f"{VENDOR_PKG}._shim",
    "_pytest.compat": f"{VENDOR_PKG}._shim",
    "_pytest.config": f"{VENDOR_PKG}._shim",
    "_pytest.fixtures": f"{VENDOR_PKG}._shim",
    "_pytest.main": f"{VENDOR_PKG}._shim",
    "_pytest.pathlib": f"{VENDOR_PKG}._shim",
    "_pytest.stash": f"{VENDOR_PKG}._shim",
    "_pytest.outcomes": f"{VENDOR_PKG}._shim",
    "_pytest._version": f"{VENDOR_PKG}._shim",
}

HEADER = """\
# ruff: noqa
# fmt: off
# Vendored from pytest — DO NOT EDIT BY HAND.
# Source: {src}
# pytest commit: {commit}
# Regenerate with: uv run python scripts/vendor_assertion.py
# Kept byte-identical to upstream apart from the edits listed in ../VENDOR.md (spec/07 Q17).
"""


@dataclass
class Edit:
    """One recorded departure from upstream, applied to exactly one vendored file."""

    file: str
    label: str
    why: str
    old: str
    new: str
    count: int = 0

    @property
    def lines_touched(self) -> int:
        return len(self.old.splitlines())


@dataclass
class Vendorer:
    edits: list[Edit] = field(default_factory=list)
    import_rewrites: int = 0

    def edit(self, file: str, label: str, why: str, old: str, new: str) -> None:
        self.edits.append(Edit(file=file, label=label, why=why, old=old, new=new))

    def apply_edits(self, file: str, src: str) -> str:
        for e in (e for e in self.edits if e.file == file):
            if e.old not in src:
                raise SystemExit(
                    f"vendoring edit no longer applies: {file}: {e.label}\n"
                    f"  looked for:\n{_indent(e.old)}\n"
                    f"  pytest has probably changed upstream; re-derive this edit by hand."
                )
            e.count = src.count(e.old)
            src = src.replace(e.old, e.new)
        return src

    def rewrite_imports(self, src: str) -> str:
        """Repoint every `_pytest.*` import at the vendored tree.

        Mechanical and total: anything left mentioning `_pytest` after this is a coupling
        point that needs a recorded edit, and `check_clean` enforces exactly that.
        """

        def repl(m: re.Match[str]) -> str:
            module = m.group("module")
            # Longest prefix wins on dot boundaries, so one `_pytest.assertion` entry covers
            # every sibling module in that package without listing each of them.
            parts = module.split(".")
            for i in range(len(parts), 0, -1):
                prefix = ".".join(parts[:i])
                if prefix in MODULE_MAP:
                    self.import_rewrites += 1
                    return m.group(0).replace(prefix, MODULE_MAP[prefix], 1)
            raise SystemExit(f"unmapped pytest import: {m.group(0)!r} — add it to MODULE_MAP")

        # `from _pytest.x.y import z` and `from _pytest import z`
        return re.sub(r"^from (?P<module>_pytest(?:\.[\w.]+)?) import ", repl, src, flags=re.M)


def _indent(text: str, prefix: str = "    | ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def build_edits(v: Vendorer) -> None:
    """The complete list of departures from upstream. Everything here is spec/07 §4."""
    # ---------------------------------------------------------------- rewrite.py: imports
    v.edit(
        "rewrite.py",
        "threading import",
        "needed by the (pid, thread)-keyed temp pyc and the pyc write lock below",
        "import sys\nimport tokenize\n",
        "import sys\nimport threading\nimport tokenize\n",
    )
    v.edit(
        "rewrite.py",
        "hashlib import",
        "needed to digest the codegen options into the pyc cache key",
        "import functools\nimport importlib.abc\n",
        "import functools\nimport hashlib\nimport importlib.abc\n",
    )
    v.edit(
        "rewrite.py",
        "TYPE_CHECKING: AssertionState from the shim",
        "velox does not vendor pytest's assertion plugin glue",
        "if TYPE_CHECKING:\n    from _pytest.assertion import AssertionState",
        f"if TYPE_CHECKING:\n    from {VENDOR_PKG}._shim import AssertionState",
    )

    # -------------------------------------------------- rewrite.py: §4.2 identity & cache
    v.edit(
        "rewrite.py",
        "injected helper-module name",
        "upstream bakes '_pytest.assertion.rewrite' into every generated pyc; a velox pyc "
        "that imports pytest's rewriter at exec time would be both wrong and a hidden dep",
        '                "_pytest.assertion.rewrite",',
        f'                "{VENDOR_PKG}.rewrite",',
    )
    v.edit(
        "rewrite.py",
        "pyc tag carries a velox rewriter revision",
        "so a change to velox's vendored codegen invalidates stale pycs, not just a CPython "
        "magic-number bump",
        "# pytest caches rewritten pycs in pycache dirs\n"
        'PYTEST_TAG = f"{sys.implementation.cache_tag}-pytest-{version}"',
        "# velox caches rewritten pycs in its own cache dir (see get_cache_dir below).\n"
        "# `version` comes from the shim and embeds VELOX_REWRITER_REVISION (spec/07 §4.2).\n"
        'PYTEST_TAG = f"{sys.implementation.cache_tag}-{version}"',
    )
    v.edit(
        "rewrite.py",
        "codegen options in the pyc cache key",
        "pytest's enable_assertion_pass_hook is a documented footgun precisely because it "
        "changes codegen but not the cache key (spec/07 §4.2)",
        "        self.session: Session | None = None\n",
        "        # velox: every option that changes generated code must be in the cache key,\n"
        "        # or a flag flip silently reuses pycs built under the old codegen.\n"
        "        self._pyc_tail = _velox_pyc_tail(config)\n"
        "        self.session: Session | None = None\n",
    )
    v.edit(
        "rewrite.py",
        "use the per-config pyc tail",
        "pairs with the edit above",
        "        cache_name = fn.name[:-3] + PYC_TAIL\n",
        "        cache_name = fn.name[:-3] + self._pyc_tail\n",
    )
    v.edit(
        "rewrite.py",
        "temp pyc keyed on (pid, thread)",
        "velox runs tests concurrently in one process; two threads importing different test "
        "modules would otherwise race on the same temp file name (spec/07 §4.2)",
        '    proc_pyc = f"{pyc}.{os.getpid()}"',
        '    proc_pyc = f"{pyc}.{os.getpid()}.{threading.get_ident()}"',
    )
    v.edit(
        "rewrite.py",
        "_writing_pyc: thread-local guard + real lock",
        "the upstream bool is both a reentrancy guard and a (non-)mutex; under threads it "
        "leaks across threads and serialises nothing (spec/07 §4.2)",
        # Must match upstream byte for byte, so this line cannot be wrapped.
        "        # flag to guard against trying to rewrite a pyc file while we are already writing another pyc file,\n"  # noqa: E501
        "        # which might result in infinite recursion (#3506)\n"
        "        self._writing_pyc = False\n",
        "        # Guard against rewriting a pyc while already writing one, which would recurse\n"
        "        # (#3506). velox: thread-local, because the guard is per-call-stack, plus a real\n"
        "        # lock so concurrent writers serialise instead of interleaving.\n"
        "        self._writing_pyc = threading.local()\n"
        "        self._pyc_write_lock = threading.Lock()\n",
    )
    v.edit(
        "rewrite.py",
        "_writing_pyc read",
        "pairs with the edit above",
        "        if self._writing_pyc:\n            return None\n",
        '        if getattr(self._writing_pyc, "active", False):\n            return None\n',
    )
    v.edit(
        "rewrite.py",
        "_writing_pyc write",
        "pairs with the edit above",
        "                self._writing_pyc = True\n"
        "                try:\n"
        "                    _write_pyc(state, co, source_stat, pyc)\n"
        "                finally:\n"
        "                    self._writing_pyc = False\n",
        "                self._writing_pyc.active = True\n"
        "                try:\n"
        "                    with self._pyc_write_lock:\n"
        "                        _write_pyc(state, co, source_stat, pyc)\n"
        "                finally:\n"
        "                    self._writing_pyc.active = False\n",
    )
    v.edit(
        "rewrite.py",
        "get_cache_dir honours velox's resolved cache root",
        "velox resolves and probes one cache root at startup (spec/07 §5) rather than "
        "scattering pycs into every source tree",
        "    if sys.pycache_prefix:",
        "    root = _velox_cache_root\n"
        "    if root is not None:\n"
        "        return root / Path(*file_path.parts[1:-1])\n"
        "    if sys.pycache_prefix:",
    )

    # ------------------------------------------------- rewrite.py: drop pytest warning types
    v.edit(
        "rewrite.py",
        "_warn_already_imported without pytest's config-time warning plumbing",
        "issue_config_time_warning is pytest plugin machinery velox does not have",
        "        from _pytest.warning_types import PytestAssertRewriteWarning\n\n"
        "        self.config.issue_config_time_warning(\n"
        "            PytestAssertRewriteWarning(\n"
        '                f"Module already imported so cannot be rewritten; {name}"\n'
        "            ),\n"
        "            stacklevel=5,\n"
        "        )",
        "        import warnings\n\n"
        "        warnings.warn(\n"
        '            f"Module already imported so cannot be rewritten; {name}",\n'
        "            VeloxAssertRewriteWarning,\n"
        "            stacklevel=5,\n"
        "        )",
    )
    v.edit(
        "rewrite.py",
        "always-true-assert warning import",
        "same",
        "            import warnings\n\n"
        "            from _pytest.warning_types import PytestAssertRewriteWarning\n\n",
        "            import warnings\n\n",
    )
    v.edit(
        "rewrite.py",
        "always-true-assert warning class",
        "same",
        "                PytestAssertRewriteWarning(\n"
        '                    "assertion is always true, perhaps remove parentheses?"\n'
        "                ),",
        "                VeloxAssertRewriteWarning(\n"
        '                    "assertion is always true, perhaps remove parentheses?"\n'
        "                ),",
    )

    # --------------------------------------------------------- rewrite.py: fresh velox block
    v.edit(
        "rewrite.py",
        "velox support block (cache root, pyc tail, warning type)",
        "fresh code, appended near the top so the rest of the file can reference it",
        "# Special marker that denotes we have just left a scope definition\n",
        VELOX_BLOCK + "\n# Special marker that denotes we have just left a scope definition\n",
    )

    # ---------------------------------------------------------- _compare_any.py: velox.approx
    v.edit(
        "_compare_any.py",
        "velox's Approx, with an optional _repr_compare",
        "velox ships its own approx (spec/07 §8); the MVP one is scalars-only and has no "
        "detailed diff to offer, so the summary line has to stand on its own",
        "        from _pytest.approx import Approx\n"
        "\n"
        "        # Although the common order should be obtained == approx(...), allow both ways.\n"
        "        if isinstance(right, Approx):\n"
        "            yield from right._repr_compare(left)\n"
        "        elif isinstance(left, Approx):\n"
        "            yield from left._repr_compare(right)\n",
        "        from velox._assertions.approx import Approx\n"
        "\n"
        "        # Although the common order should be obtained == approx(...), allow both ways.\n"
        "        # velox: _repr_compare is optional; a scalar approx has no diff worth showing.\n"
        "        if isinstance(right, Approx):\n"
        "            yield from _velox_approx_compare(right, left)\n"
        "        elif isinstance(left, Approx):\n"
        "            yield from _velox_approx_compare(left, right)\n",
    )
    v.edit(
        "_compare_any.py",
        "_velox_approx_compare helper",
        "fresh, so the branch above stays readable",
        "def _compare_eq_any(\n",
        "def _velox_approx_compare(approx: object, other: object) -> Iterator[str]:\n"
        '    """Detailed lines for an approx comparison, if this Approx can produce any."""\n'
        '    repr_compare = getattr(approx, "_repr_compare", None)\n'
        "    if repr_compare is None:\n"
        "        return iter(())\n"
        "    return repr_compare(other)\n"
        "\n"
        "\n"
        "def _compare_eq_any(\n",
    )

    # ------------------------------------------------------------ util.py: §4.1 ContextVars
    v.edit(
        "util.py",
        "the three module globals become ContextVars",
        "THE concurrency blocker: pytest save/restores these per test item, which cannot work "
        "when tests are concurrent asyncio tasks in one process (spec/07 §4.1)",
        "# The _reprcompare attribute on the util module is used by the new assertion\n"
        "# interpretation code and assertion rewriter to detect this plugin was\n"
        "# loaded and in turn call the hooks defined here as part of the\n"
        "# DebugInterpreter.\n"
        "_reprcompare: Callable[[str, object, object], str | None] | None = None\n"
        "\n"
        "# Works similarly as _reprcompare attribute. Is populated with the hook call\n"
        "# when pytest_runtest_setup is called.\n"
        "_assertion_pass: Callable[[int, str, str], None] | None = None\n"
        "\n"
        "# Config object which is assigned during pytest_runtest_protocol.\n"
        "_config: Config | None = None\n",
        UTIL_CONTEXTVAR_BLOCK,
    )
    v.edit(
        "util.py",
        "crash repr without _pytest._code",
        "pytest's ExceptionInfo is a large subsystem; the failure path here only needs a "
        "one-line 'where did the repr blow up' string",
        "        repr_crash = _pytest._code.ExceptionInfo.from_current()._getreprcrash()",
        "        repr_crash = _shim_repr_crash()",
    )
    v.edit(
        "util.py",
        "drop the bare `import _pytest._code`",
        "pairs with the edit above; the generic import rewriter only handles `from` imports",
        "import _pytest._code\n",
        f"from {VENDOR_PKG}._shim import repr_crash as _shim_repr_crash\n",
    )
    v.edit(
        "util.py",
        "velox naming in the repr-failure message",
        "the string is user-visible; it should not say 'pytest_assertion plugin'",
        '            f"(pytest_assertion plugin: representation of details failed: {repr_crash}."',
        '            f"(velox: representation of details failed: {repr_crash}."',
    )


# Fresh code inserted into rewrite.py. Kept as one labelled block so the upstream diff stays
# one hunk rather than being sprinkled through the file.
VELOX_BLOCK = '''
# --------------------------------------------------------------------------- velox additions
class VeloxAssertRewriteWarning(UserWarning):
    """Warned when a module could not be rewritten, or an assert looks always-true."""


#: Set by velox._assertions.rewrite once the cache root has been resolved and probed (spec/07 §5).
#: None means "fall back to sys.pycache_prefix / __pycache__", i.e. upstream behaviour.
_velox_cache_root: Path | None = None

#: Config options that change generated code, and so must be part of the pyc cache key.
#: Adding a codegen flag without adding it here is exactly pytest's documented footgun.
VELOX_CODEGEN_OPTIONS = ("enable_assertion_pass_hook",)


def set_cache_root(root: Path | None) -> None:
    """Point the pyc cache at `root`, or back at upstream behaviour with None."""
    global _velox_cache_root
    _velox_cache_root = root


def _velox_pyc_tail(config: Config) -> str:
    """The pyc filename suffix for `config`, keyed on every codegen-affecting option."""
    parts = []
    for name in VELOX_CODEGEN_OPTIONS:
        try:
            value = config.getini(name)
        except (ValueError, KeyError):
            value = None
        parts.append(f"{name}={value!r}")
    digest = hashlib.blake2b("\\n".join(parts).encode(), digest_size=4).hexdigest()
    # PYTEST_TAG already carries the rewriter revision, via the shim's `version`.
    return f".{PYTEST_TAG}-{digest}{PYC_EXT}"

'''

UTIL_CONTEXTVAR_BLOCK = """\
# velox: upstream these were plain module globals that pytest save/restored around each test
# item — the one genuine concurrency blocker in this subsystem, since velox runs tests as
# concurrent asyncio tasks sharing a process (spec/07 §4.1). They are ContextVars now, read
# through a PEP 562 module __getattr__ so the vendored rewriter's `util._reprcompare` lookups
# are untouched. Do NOT assign to these names: a real global would shadow __getattr__ and
# silently restore the old, unsafe behaviour. Use velox._assertions.state instead.
from velox._assertions.state import CONTEXT_GLOBALS as _velox_context_globals


def __getattr__(name: str) -> object:
    try:
        var = _velox_context_globals[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    return var.get()
"""


def pytest_commit() -> str:
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(REPO / "oss" / "pytest"),
                "describe",
                "--tags",
                "--always",
                "--dirty",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def check_clean(name: str, src: str) -> None:
    """No vendored file may still reference pytest at runtime."""
    leftovers = [
        f"{i}: {line.strip()}"
        for i, line in enumerate(src.splitlines(), 1)
        if "_pytest" in line and not line.lstrip().startswith("#")
    ]
    if leftovers:
        raise SystemExit(f"{name} still references _pytest:\n" + "\n".join(leftovers))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the vendored tree matches what this script would generate, and write nothing",
    )
    args = parser.parse_args()

    if not PYTEST_SRC.is_dir():
        raise SystemExit(
            f"{PYTEST_SRC} not found — the pytest submodule is not checked out.\n"
            "Run: git submodule update --init oss/pytest"
        )

    v = Vendorer()
    build_edits(v)
    commit = pytest_commit()
    generated: dict[str, str] = {}

    for rel, out_name in FILES.items():
        src = (PYTEST_SRC / rel).read_text()
        src = v.apply_edits(out_name, src)
        src = v.rewrite_imports(src)
        check_clean(out_name, src)
        generated[out_name] = HEADER.format(src=f"_pytest/{rel}", commit=commit) + src

    unapplied = [e for e in v.edits if e.count == 0]
    if unapplied:
        raise SystemExit(
            "edits declared but never applied: " + ", ".join(e.label for e in unapplied)
        )

    if args.check:
        # Only the vendored sources are compared. VENDOR.md records *when* the vendoring
        # happened, so checking it would fail every day for no reason; what matters is that
        # nobody hand-edited the tree and that the submodule has not moved under it.
        stale = [
            name
            for name, content in generated.items()
            if not (DST / name).exists() or (DST / name).read_text() != content
        ]
        if stale:
            print("vendored tree is stale: " + ", ".join(sorted(stale)), file=sys.stderr)
            print("run: uv run python scripts/vendor_assertion.py", file=sys.stderr)
            return 1
        print(f"vendored tree is up to date with pytest {commit}")
        return 0

    DST.mkdir(parents=True, exist_ok=True)
    for name, content in generated.items():
        (DST / name).write_text(content)
    (DST / "../VENDOR.md").write_text(render_vendor_md(v, commit, generated))

    total = sum(len(c.splitlines()) for c in generated.values())
    print(f"vendored {len(FILES)} files ({total} lines) from pytest {commit}")
    print(f"{len(v.edits)} recorded edits, {v.import_rewrites} import rewrites")
    print(f"wrote {VENDOR_MD.relative_to(REPO)}")
    return 0


def render_vendor_md(v: Vendorer, commit: str, generated: dict[str, str]) -> str:
    lines = [
        "# Vendored: pytest assertion subsystem",
        "",
        "**Generated — do not edit.** Regenerate with `uv run python scripts/vendor_assertion.py`.",
        "",
        f"- Upstream: <https://github.com/pytest-dev/pytest> at `{commit}`",
        f"- Vendored: {datetime.now(UTC).date().isoformat()}",
        f"- Destination: `{DST.relative_to(REPO)}/`",
        "",
        "Why vendored rather than reimplemented: see [spec/07](../../spec/07-assertions.md) §1.",
        "The 5.8k lines of upstream tests covering this feature are the asset; keeping the code",
        "byte-identical apart from the edits below keeps them applicable, and keeps",
        "re-vendoring cheap.",
        "",
        "## Files",
        "",
        "| Vendored | Upstream | Lines |",
        "|---|---|---|",
    ]
    for rel, out_name in FILES.items():
        n = len(generated[out_name].splitlines())
        lines.append(f"| `{out_name}` | `_pytest/{rel}` | {n} |")
    lines += [
        "",
        "Not vendored: `_pytest/assertion/__init__.py` (pytest's plugin glue). velox's equivalent",
        "is `velox/_assertions/rewrite.py`. Everything pytest-specific those files imported is",
        "replaced by",
        f"`{DST.relative_to(REPO)}/_shim.py` (~130 LOC, hand-written).",
        "",
        "## Applied edits",
        "",
        f"{len(v.edits)} recorded edits touching "
        f"{sum(e.lines_touched for e in v.edits)} upstream lines, plus "
        f"{v.import_rewrites} mechanical `_pytest.*` import rewrites.",
        "",
    ]
    for file in FILES.values():
        file_edits = [e for e in v.edits if e.file == file]
        if not file_edits:
            continue
        lines += [f"### `{file}`", ""]
        for e in file_edits:
            lines.append(
                f"- **{e.label}** (x{e.count}, {e.lines_touched} upstream lines) — {e.why}"
            )
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
