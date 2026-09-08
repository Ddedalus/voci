# `cli.py` — entrypoint

See [rationale.md](../rationale.md) for the index.

**Flags default to `None`, not to their real defaults.** Layering CLI over `[tool.voci]` over the
built-in default requires distinguishing "the user typed `--concurrency`" from "argparse filled one
in". A concrete argparse default erases that distinction and makes the config file's value
unreachable whenever the two happen to match.

**`main()` leaves no global state behind, on any exit path.** It installs the rewrite import hook,
prepends `rootdir` to `sys.path`, and applies `[tool.voci] env` to `os.environ` — and undoes all
three in one `finally`, each restoring only what this call changed rather than resetting to a fixed
state. `main()` is called repeatedly in-process (this package's own suite does it), so a missed
restore leaks a stale `sys.path` entry shadowing a same-named package, or one suite's environment
into the next.

**`--basetemp` is validated in two places on purpose.** `cli.py` does a cheap path-shape check (cwd,
ancestors, home, root) so a typo fails fast and clean; `_capture.install` checks for its marker
file. Both guard the same catastrophe — an unguarded `rmtree` on a wrong path — and neither
subsumes the other, since only CLI callers reach the first and only the second protects callers
that use `_capture`/`_run` directly.
