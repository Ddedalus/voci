"""Installing assertion introspection: cache resolution, the import hook, the explanation hook.

This is velox's replacement for `_pytest/assertion/__init__.py` — the plugin glue, not the
rewriter. The rewriter itself is vendored (`velox/_vendor/assertion/`) and untouched apart from
the edits in `VENDOR.md`.

Two things here are velox's own, not ports:

* **The cold-start guarantee.** Rewriting costs 4.6x on a cold import and 1/154th of that warm,
  so the pyc cache is load-bearing, not an optimisation. velox resolves one cache root, probes it
  once, and if it is unwritable says so on stderr and drops to `plain` — rather than silently
  paying 4.6x on every run in a CI container.
* **Which modules get rewritten.** pytest rewrites files matching `test_*.py` plus conftests;
  assertions in `tests/fixtures.py` get nothing. velox rewrites every `.py` file discovered under
  the test roots, which is a superset, and costs one path check.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from velox._assertion_state import assertion_state
from velox._vendor.assertion import rewrite as _rewrite
from velox._vendor.assertion import truncate as _truncate
from velox._vendor.assertion import util as _util
from velox._vendor.assertion._shim import AssertionState, Config, Stash, StashKey
from velox._vendor.assertion._typing import NO_TRUNCATION_BUDGET, TruncationBudget

__all__ = [
    "AssertMode",
    "AssertionSetup",
    "Config",
    "Stash",
    "assertion_context",
    "compare_explanation",
    "explanation_lines",
    "install",
    "installed_hook",
    "plan",
    "resolve_cache_dir",
    "strip_rewriter_temps",
    "uninstall",
]

#: `rewrite` is the default; `plain` keeps bare asserts and leans on the PEP 657 floor
#: (`velox/_pep657.py`), which is why `plain` is a usable mode rather than a punishment.
type AssertMode = Literal["rewrite", "plain"]

ENV_CACHE_DIR = "VELOX_REWRITE_CACHE"

#: Written and deleted to prove the cache root is usable before anything depends on it.
_PROBE_NAME = ".velox-write-probe"


@dataclass(frozen=True, slots=True)
class AssertionSetup:
    """What assertion introspection actually ended up doing, for the report header.

    A benchmark run that quietly fell back to `plain` is a corrupted benchmark, so the
    fallback is recorded rather than merely warned about.
    """

    mode: AssertMode
    #: None in `plain` mode, or when the resolved root failed its probe.
    cache_dir: Path | None
    #: Human-readable reason the requested mode was not honoured; None if it was.
    fallback_reason: str | None = None
    rewritten_roots: tuple[Path, ...] = field(default=())

    @property
    def degraded(self) -> bool:
        return self.fallback_reason is not None

    def header_line(self) -> str:
        """One line for the report header. Always emitted, so `rewrite` is visibly the norm."""
        if self.degraded:
            return f"assertions: {self.mode} (fallback: {self.fallback_reason})"
        if self.cache_dir is None:
            return f"assertions: {self.mode}"
        return f"assertions: {self.mode}, cache {self.cache_dir}"


# --------------------------------------------------------------------------- cache location


def resolve_cache_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Where rewritten pycs go: an explicit path, then `VELOX_REWRITE_CACHE`, then the platform's
    cache directory, then the interpreter's own pycache prefix, then a `.velox_cache` under `cwd()`.

    Does not check writability — `_probe_writable` does that, separately, so a caller can
    report the path it tried even when the probe fails.
    """
    if explicit is not None:
        return Path(explicit).expanduser()

    from_env = os.environ.get(ENV_CACHE_DIR)
    if from_env:
        return Path(from_env).expanduser()

    platform_cache = _platform_cache_dir()
    if platform_cache is not None:
        return platform_cache / "velox" / "rewrite"

    # Last resort: no home directory to put a cache in (containers often have none), so
    # borrow wherever the interpreter was already told to put pycs, in a velox-owned subdir.
    if sys.pycache_prefix:
        return Path(sys.pycache_prefix) / "velox-rewrite"

    return Path.cwd() / ".velox_cache" / "rewrite"


def _platform_cache_dir() -> Path | None:
    """The OS's conventional user cache directory, or None if there isn't one to speak of."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) if local else None
    if sys.platform == "darwin":
        home = os.environ.get("HOME")
        return Path(home) / "Library" / "Caches" if home else None
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg)
    home = os.environ.get("HOME")
    return Path(home) / ".cache" if home else None


def _probe_writable(cache_dir: Path) -> str | None:
    """Create and delete a marker in `cache_dir`. Returns None if fine, else why not.

    Done once at startup rather than discovered per-module: the failure mode this guards
    against is a read-only or missing cache silently turning every run into a cold run, and
    that has to be visible before the run, not inferred from its timings.
    """
    # A mistyped `--rewrite-cache` shouldn't litter: if `cache_dir` didn't exist before this
    # call, it was created solely to run the probe, and velox falls back to `plain` and never
    # uses it — so a probe failure below removes it again rather than leaving an empty tree.
    pre_existing = cache_dir.exists()
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"cannot create {cache_dir}: {exc.strerror or exc}"

    probe = cache_dir / _PROBE_NAME
    try:
        probe.write_bytes(b"velox")
        probe.unlink()
    except OSError as exc:
        if not pre_existing:
            shutil.rmtree(cache_dir, ignore_errors=True)
        return f"{cache_dir} is not writable: {exc.strerror or exc}"
    return None


# ------------------------------------------------------------------------------ the session


@dataclass(frozen=True, slots=True)
class _DiscoveredPaths:
    """The `Session` the vendored rewriter asks about, holding every discovered test file.

    `_initialpaths` feeds two things upstream, unmodified: `isinitpath`, which forces a rewrite
    regardless of filename, and the early-bailout's basename set, which stops those same files
    being skipped before the path is ever consulted. Handing it every discovered `.py` file —
    `fixtures.py`, `helpers.py`, `conftest.py`, and the `test_*.py` files alike — makes "rewrite
    everything under the test roots" fall out of the vendored logic with no change to it.
    """

    _initialpaths: frozenset[Path]

    def isinitpath(self, path: Path) -> bool:
        return path in self._initialpaths


#: Directories never worth descending into by name alone, regardless of what's inside them.
_PRUNED_DIR_NAMES = frozenset({"__pycache__", "node_modules", "site-packages"})


def _prune_dir(path: Path, skip_roots: frozenset[Path]) -> bool:
    """Whether `path` (a directory found during the walk) should not be descended into.

    Dot-directories (`.git`, `.venv`, `.mypy_cache`, ...), `__pycache__`, `node_modules`, and
    `site-packages` are skipped by name alone — cheap, and covers the overwhelming majority of a
    project tree that is not the user's own code. A `pyvenv.cfg` catches virtualenvs not named
    `.venv`, and `skip_roots` catches the interpreter's own install prefix, in case a root is
    broad enough to reach it without going through a named venv directory at all.
    """
    name = path.name
    if name.startswith(".") or name in _PRUNED_DIR_NAMES:
        return True
    if path in skip_roots:
        return True
    return (path / "pyvenv.cfg").is_file()


def _discover_python_files(roots: Iterable[Path]) -> frozenset[Path]:
    """Every `.py` file under `roots`, absolutely-pathed. Files are taken as-is.

    Walked with `os.walk`, which allows pruning `dirnames` in place, unlike `Path.rglob`. Every
    discovered file lands in `_initialpaths`, which not only forces a rewrite (`isinitpath`) but
    also feeds the rewriter's name-based early-bailout set — pruning here is what keeps velox from
    recompiling and rewriting asserts across the entire installed dependency tree on a cold run.
    """
    found: set[Path] = set()
    skip_roots = frozenset(
        Path(p).resolve() for p in (sys.prefix, sys.base_prefix, sys.exec_prefix) if p
    )
    for root in roots:
        root = Path(os.path.abspath(root))
        if root.is_file():
            found.add(root)
            continue
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            current = Path(dirpath)
            dirnames[:] = [d for d in dirnames if not _prune_dir(current / d, skip_roots)]
            found.update(current / name for name in filenames if name.endswith(".py"))
    return frozenset(found)


# ----------------------------------------------------------------------------- installation

assertstate_key: StashKey[AssertionState] = _rewrite.assertstate_key


def plan(
    roots: Iterable[Path | str] = (),
    *,
    mode: AssertMode = "rewrite",
    cache_dir: str | os.PathLike[str] | None = None,
    warn: bool = True,
) -> AssertionSetup:
    """Decide what assertion introspection will do, without installing anything.

    Separated from `install` so the decision — including the cache probe and its warning — can
    be made and reported before a run commits to it, and so it can be tested without touching
    `sys.meta_path`.
    """
    # `AssertMode` is a `Literal`, which covers argparse's `choices=` path (see `cli.py`) but not
    # a programmatic caller's typo or a config file read without going through argparse at all —
    # those reach this function as a plain `str`, so an unrecognised value is checked explicitly
    # rather than falling through to "rewrite" by default.
    if mode not in ("rewrite", "plain"):
        raise ValueError(f"unknown assertion mode {mode!r}; expected 'rewrite' or 'plain'")

    root_paths = tuple(Path(os.path.abspath(r)) for r in roots)

    if mode == "plain":
        return AssertionSetup(mode="plain", cache_dir=None, rewritten_roots=root_paths)

    resolved = resolve_cache_dir(cache_dir)
    problem = _probe_writable(resolved)
    if problem is None:
        return AssertionSetup(mode="rewrite", cache_dir=resolved, rewritten_roots=root_paths)

    if warn:
        # Naming the path is the point: "assertion rewriting is off" is not actionable,
        # "this directory is not writable" is.
        print(
            f"velox: assertion rewriting disabled — {problem}\n"
            f"velox: falling back to --assert=plain (PEP 657 caret spans only). "
            f"Set {ENV_CACHE_DIR} or --rewrite-cache to a writable path.",
            file=sys.stderr,
        )
    return AssertionSetup(
        mode="plain",
        cache_dir=None,
        fallback_reason=problem,
        rewritten_roots=root_paths,
    )


#: What the currently-installed hook's `install()` call decided, so a later idempotent call —
#: or `uninstall` — has something to report or clear. `None` whenever no hook is installed.
_installed_setup: AssertionSetup | None = None


def install(
    roots: Iterable[Path | str] = (),
    *,
    mode: AssertMode = "rewrite",
    cache_dir: str | os.PathLike[str] | None = None,
    verbosity: int = 0,
    ini: dict[str, object] | None = None,
    trace: object = None,
    warn: bool = True,
    setup: AssertionSetup | None = None,
) -> AssertionSetup:
    """Put the rewriting import hook at the front of `sys.meta_path`.

    Must run before any test module is imported — a module already in `sys.modules` cannot be
    rewritten, and the hook warns rather than silently doing nothing.

    Idempotent: if a hook is already installed, this is a no-op that returns the setup the first
    call decided on. Without that guard a second call would push a second hook onto
    `sys.meta_path` — two rewrites per import, and two competing cache roots, since
    `set_cache_root` is module-global and the last caller wins.

    Pass `setup` when the caller already ran `plan` — `cli.main` does, to print the report
    header line — so the cache probe (and its stderr warning on a fallback) runs exactly once
    instead of twice with the same two lines. Without `setup`, `install` calls `plan` itself and
    forwards `warn`.

    Returns what actually happened, including any fallback. In `plain` mode, or after a failed
    cache probe, nothing is installed and the PEP 657 floor carries the whole load.
    """
    global _installed_setup

    if installed_hook() is not None:
        assert _installed_setup is not None, "a hook is installed but its setup was not recorded"
        return _installed_setup

    if setup is None:
        setup = plan(roots, mode=mode, cache_dir=cache_dir, warn=warn)
    if setup.mode == "plain":
        return setup

    assert setup.cache_dir is not None
    root_paths = setup.rewritten_roots
    resolved = setup.cache_dir

    config = Config(ini, verbosity=verbosity)
    config.stash[assertstate_key] = AssertionState(mode="rewrite", trace=trace)

    _rewrite.set_cache_root(resolved)
    hook = _rewrite.AssertionRewritingHook(config)
    hook.set_session(_DiscoveredPaths(_discover_python_files(root_paths)))
    # Front of the chain: the rewriter must win over the default path finder, and its
    # name-based early bailout is what keeps that cheap for every non-test import.
    sys.meta_path.insert(0, hook)

    _installed_setup = setup
    return setup


def uninstall(hook: object | None = None) -> None:
    """Remove velox's rewriting hook(s) from `sys.meta_path`. Idempotent."""
    global _installed_setup
    for entry in list(sys.meta_path):
        if isinstance(entry, _rewrite.AssertionRewritingHook) and (hook is None or entry is hook):
            sys.meta_path.remove(entry)
    _rewrite.set_cache_root(None)
    _installed_setup = None


def installed_hook() -> _rewrite.AssertionRewritingHook | None:
    """The rewriting hook currently on `sys.meta_path`, if any."""
    for entry in sys.meta_path:
        if isinstance(entry, _rewrite.AssertionRewritingHook):
            return entry
    return None


# ------------------------------------------------------------------- the explanation hook


def explanation_lines(
    op: str, left: object, right: object, config: Config | None = None
) -> list[str] | None:
    """The detailed diff for `left op right`, as lines, or None when there is nothing to add.

    This is the shape pytest's `pytest_assertrepr_compare` returns, kept deliberately so the
    ported upstream tests exercise velox's wiring rather than a paraphrase of it.

    Pre-budgets the formatting to what the truncator will actually keep, so an enormous diff is
    never built in full just to be thrown away.
    """
    config = config if config is not None else Config()

    should_truncate, base = _truncate._get_truncation_parameters(config)
    if should_truncate:
        budget = TruncationBudget(
            max_lines=(
                base.max_lines + _truncate.TRUNCATION_FOOTER_LINES + 1 if base.max_lines > 0 else 0
            ),
            max_chars=(
                base.max_chars + _truncate.TRUNCATION_FOOTER_CHARS if base.max_chars > 0 else 0
            ),
        )
    else:
        budget = NO_TRUNCATION_BUDGET

    lines = _util.assertrepr_compare(
        op=op,
        left=left,
        right=right,
        verbose=config.get_verbosity(Config.VERBOSITY_ASSERTIONS),
        highlighter=_util.dummy_highlighter,
        assertion_text_diff_style=config.getini("assertion_text_diff_style"),
        truncation_budget=budget,
    )
    return _truncate.materialize_with_truncation(lines, config) or None


def compare_explanation(op: str, left: object, right: object, config: Config | None) -> str | None:
    """velox's `_reprcompare`: what the rewriter splices into a failed comparison's message."""
    explanation = explanation_lines(op, left, right, config)
    if explanation is None:
        return None

    # Newlines are escaped so `format_explanation` can use them as its own structure markers,
    # and `%` is doubled because the rewriter's explanation is a %-format template.
    escaped = [line.replace("\n", "\\n") for line in explanation]
    return "\n~".join(escaped).replace("%", "%%")


def assertion_context(config: Config | None = None) -> AbstractContextManager[None]:
    """Bind the explanation hook for the current context. Use around each test.

    ContextVar-scoped, so concurrent tests never see each other's config.
    """
    resolved = config if config is not None else Config()
    return assertion_state(
        config=resolved,
        reprcompare=lambda op, left, right: compare_explanation(op, left, right, resolved),
    )


# ---------------------------------------------------------------------------------- locals

#: The rewriter hoists every subexpression into a temp named `@py_assert3`, `@py_format7`,
#: etc. The `@` makes them unreachable as identifiers, and useless in a locals display.
_TEMP_PREFIX = "@py"


def strip_rewriter_temps(variables: dict[str, object]) -> dict[str, object]:
    """Drop the rewriter's scratch variables from a frame's locals.

    Without this, every failure in a rewritten module shows a wall of `@py_assert*` bindings
    above the ones the user wrote.
    """
    return dict(iter_user_locals(variables))


def iter_user_locals(variables: dict[str, object]) -> Iterator[tuple[str, object]]:
    """`strip_rewriter_temps` as a stream, for reporting paths that never materialise a dict."""
    for name, value in variables.items():
        if not name.startswith(_TEMP_PREFIX):
            yield name, value
