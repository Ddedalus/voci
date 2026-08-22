# Docs site plan

Internal working document. Plans a narrative-docs-plus-reference site for velox, structured and
themed like [FastAPI's docs](../oss/fastapi/docs/en) (cloned at `oss/fastapi/` for reference). 

## What "same look and feel" means

FastAPI's docs run on [mkdocs](https://www.mkdocs.org/) + mkdocs-material, with content split
into four kinds of page:

- **Tutorial** (`tutorial/`) — ordered, narrative, one concept per page, each built around a
  runnable code sample.
- **How-to** (`how-to/`) — short, task-focused recipes assuming the tutorial already happened.
- **Reference** (`reference/`) — [mkdocstrings](https://mkdocstrings.github.io/)-generated API
  pages, one per public symbol group, rendered straight from docstrings.
- **About** (`about/`, plus `index.md`) — what it is, why, and orientation.

The generator is [Zensical](https://zensical.org/), not mkdocs. mkdocs has had no maintainer
since August 2024, which is why the Material for MkDocs team built Zensical as its successor
rather than fork it; FastAPI's own build (`oss/fastapi/scripts/docs.py`) already runs a real
production Zensical build alongside its mkdocs one, reading the same `mkdocs.yml`. Adopting this
for velox means: Zensical as the site generator, its **modern** theme (Zensical's default —
a fresh design, not a clone of Material's chrome), the same four-way split, and mkdocstrings for
the reference section so it stays truthful to the source instead of hand-duplicated.

Zensical is alpha software (`Development Status :: 3 - Alpha` on PyPI, versions still `0.0.x`),
under active development — multiple releases a week as of this writing. mkdocstrings support is
explicitly "preliminary" (its author joined the Zensical team to build it, but some features,
named as backlinks, aren't there yet); the wider plugin ecosystem (macros, tags, redirects,
social cards) is still filling in. None of the gaps found so far touch what this plan needs —
mkdocstrings' Python handler, and the pymdown-extensions family (admonitions, superfences,
snippets) are both supported today, and are already default-enabled. Accepted trade-off: pin the
version and expect breaking changes on upgrade, the way any alpha dependency is handled.

## Where the site lives

Internal working documents (this file included) live in `plans/`, not `docs/`.

```
zensical.toml                     repo root — see below for why
docs/
  rationale.md                    existing, unchanged — canonical WHY doc, cited by README/CLAUDE.md
  index.md                        home page, adapted from README.md
  guide/                          = FastAPI's tutorial/
  how-to/
  reference/
  about/
  img/  css/  js/
```

`zensical.toml` lives at the repo root, not inside `docs/`: `docs_dir` defaults to `docs`
relative to the config file, and Zensical's docs_dir currently cannot be set to `.` — so putting
the config next to `docs/` (the same place `pyproject.toml` and `justfile` already sit) gets the
default for free, with no config file inside the published tree and no `docs/en/` locale layer
(velox ships one language).

Native format is `zensical.toml` (TOML), not `mkdocs.yml` — nothing here is migrating from an
existing mkdocs project, and TOML already matches this repo's other config (`pyproject.toml`).
Zensical's `mkdocs.yml` compatibility layer is a permanent feature, not a deprecation trap, so
this isn't a one-way door if it turns out to matter.

`rationale.md` needs no symlink or move: it already sits inside `docs_dir`, at the path README
and `CLAUDE.md` already cite, and the site's nav just groups it under **About** without touching
where the file lives.

## Navigation

Each page here is real content mapped to something velox already does — nothing on this list
describes unbuilt behavior (`ROADMAP.md` stays the only place that happens).

```
Home                    index.md
Guide                   ("" section, mirrors tutorial/)
  guide/index.md          install, first test, shape of a suite
  guide/first-steps.md    a test file, `velox`, reading the report
  guide/fixtures.md       @velox.fixture, Depends, injection syntax
  guide/scopes.md         call/function/module/session, teardown order
  guide/use.md            velox.use(...) — side-effect fixtures on a module/package
  guide/parametrize.md    @velox.parametrize, stacking, params= on a fixture
  guide/marks.md          skip, skipif, xfail, tag
  guide/concurrency.md    the semaphore, per-test timeouts, @velox.timeout
  guide/exclusive.md      exclusive=, @velox.solo, admission control
  guide/isolated.md       @velox.isolated's subprocess tier
  guide/mocking.md        unittest.mock: what's free, what costs a solo run
  guide/assertions.md     rewritten assert, raises, approx
  guide/capture.md        stdout/stderr/logging capture, tmp_path
  guide/selection.md      ids, -k, -m/@velox.tag, -x/--maxfail, --serial, --collect-only
  guide/config.md         [tool.velox], CLI-over-config-over-defaults
  guide/fastapi.md        velox.fastapi: per-test dependency overrides
How-to                  (task recipes, assumes the guide; seeded from examples/)
  how-to/index.md
  how-to/sharing-a-database-engine.md      session-scoped engine, function-scoped rollback
  how-to/testing-a-fastapi-app.md          velox.fastapi walkthrough
  how-to/tuning-concurrency.md             reading --durations, setting concurrency
  how-to/debugging-a-flaky-test.md         --serial, -x, --loop-watchdog
Reference               (mkdocstrings, one page per __init__.py export group)
  reference/index.md
  reference/fixtures.md    fixture, Fixture, Depends, Injection, Scope, use
  reference/marks.md       skip, skipif, xfail, tag, timeout, solo, isolated, parametrize + records
  reference/builtins.md    tmp_path, capture, log_records, test_info + their types
  reference/assertions.md  raises, approx, ExceptionInfo, Approx
  reference/fastapi.md     velox.fastapi: client, lifespan, uninstall
  reference/cli.md         generated from `velox --help` (see below; not mkdocstrings — argparse
                            has no docstrings to render)
About
  about/index.md           orientation, links to guide/reference
  rationale.md             already at docs/rationale.md — nav just groups it under About
  about/alternatives.md    the one neutral pytest comparison from README.md, not expanded
                            ("Don't argue with pytest" — velox-docs skill)
```

Every guide/how-to page must pass the velox-docs skill's rules: no counterfactuals, no `spec/`
citations, no ROADMAP content, docstring register matched in reference pages because it's pulled
verbatim from source.

## Code samples: reuse `examples/`, don't build a parallel `docs_src/`

Plan: guide pages carry short, hand-written inline snippets (a fixture, a test — a few lines,
matching the density already in `README.md`'s own example), and each page links out to the
`examples/` suite that demonstrates the fully worked version.

Trade-off accepted: inline snippets aren't independently test-run the way `docs_src/` files are
in FastAPI. Mitigation — keep every inline snippet short enough to eyeball against the
`reference/` page for the symbols it uses (which *is* generated from live source), and prefer
lifting a snippet verbatim from an `examples/` file (with a line-range comment noting the source)
over writing a new one from scratch.

Embedding mechanism: `pymdownx.snippets` — pymdown-extensions is default-enabled under Zensical,
same as under mkdocs-material — to pull a marked block out of an `examples/` file into a fenced
code block.

## Reference pages: mkdocstrings

Same mechanism as FastAPI: `mkdocstrings[python]`, `::: velox.fixture`-style directives, reading
docstrings that already follow the velox-docs register. `filters: ['!^_']` to keep private names
out, `show_root_heading`, `merge_init_into_class`, `signature_crossrefs` — the same options
FastAPI sets, since they're generic mkdocstrings behavior, not Zensical- or FastAPI-specific.
Configured under `[project.plugins.mkdocstrings.handlers.python]` in `zensical.toml`, per
[Zensical's mkdocstrings docs](https://zensical.org/docs/setup/extensions/mkdocstrings/).

`reference/cli.md` is the one page mkdocstrings can't produce — argparse has no docstring-driven
autodoc path. Generate it the way `velox/_assertions/_vendor/` is generated: a script
(`scripts/gen_cli_reference.py`) that imports `velox.cli.build_parser()` and renders its help text
into the page, plus a `--check` mode wired into `just docs check` the same way `just vendor check`
guards the vendored tree, so the reference can't drift from the real flags.

## Theme

`variant = "modern"` — Zensical's default, so this needs no config beyond stating it explicitly
against a future default change. It's a different, newer design than Material's classic chrome,
so FastAPI's `theme:` feature list (`navigation.tabs`, `toc.follow`, and so on — all classic-variant
options) doesn't carry over; modern's own defaults are the baseline, customized only once there's
a concrete reason to. Search is built in (Zensical's own client-side engine), no plugin needed.
`logo`/`favicon` skipped for v1 — no brand art exists yet; flag as a follow-up.

## Tooling

- `pyproject.toml`: new `[dependency-groups] docs` — `zensical`, `mkdocstrings[python]` (same
  pairing FastAPI's own `pyproject.toml` uses).
- `justfile`: `docs-serve` (`zensical serve`), `docs-build` (`zensical build`), `docs-check`
  (`gen_cli_reference.py --check`, then `docs-build`) — same shape as the existing
  `vendor`/`vendor-check` pair. Both read `zensical.toml` from the repo root by default, so no
  `-f`/`--config-file` flag is needed as long as `just` runs recipes from the root.
- A broken internal link fails the build, the way `mkdocs build --strict` did: `strict = true` in
  `zensical.toml` (equivalently `zensical build -s`), confirmed against a link to a page that
  doesn't exist.

## Phasing

1. ~~**Skeleton**~~ — **done.** `zensical.toml`, theme config, `index.md` adapted from
   `README.md`, an `index.md` per section, `docs-serve`/`docs-build` recipes, `docs` dependency
   group. What it settled:
   - `strict = true` in `zensical.toml` is the `mkdocs build --strict` equivalent, and it does
     fail the build on a link to a page that doesn't exist. `zensical build -s` is the same
     switch from the CLI.
   - Link validation is scoped to `docs_dir`, so `docs/rationale.md`'s `../README.md` and
     `../ROADMAP.md` links failed the build. They became a link to `guide/index.md` and an
     unlinked mention of `ROADMAP.md`. Every later page has to reach `examples/`, `README.md` or
     `ROADMAP.md` the same way — named, not linked out of the tree — until phase 6 settles
     `repo_url`.
   - The modern variant renders light-only unless `[[project.theme.palette]]` entries are
     declared; the three from Zensical's own starter config (system/light/dark) put the toggle in
     the header.
   - `navigation.indexes` is the one theme feature enabled, so `guide/index.md` *is* the Guide nav
     entry rather than a lone child of it.
   - `docs` joins `dev` in `[tool.uv] default-groups`. Left out, `just docs build` installs the
     docs toolchain and the next `just sync` uninstalls it again.
   - Zensical is pinned exactly (`zensical==0.0.56`), per the alpha trade-off above.
   - `guide/index.md` is written for real rather than stubbed — install, first test, shape of a
     suite — since a placeholder would have to describe a page that doesn't exist.
2. **Reference** — mkdocstrings wired up, the five symbol-group pages, `reference/cli.md` plus its
   generator script and check recipe.
3. **Guide** — the 14 pages above, each with its inline snippet and a link into the matching
   `examples/` suite.
4. **How-to** — the four recipe pages, each derived from a specific `examples/` file.
5. **About** — `index.md`, a nav entry for the existing `rationale.md`, `alternatives.md`.
6. **CI** — `docs-check` into `just check`; hosting (GitHub Pages or otherwise) is a follow-up
   decision once the repo is public — out of scope here.

Each phase is a reviewable unit on its own branch, per the worktree workflow.

## Open questions

- **Hosting.** Not decided here — the repo isn't public yet, so there's nowhere to point a
  `site_url`/`repo_url` at. Revisit at phase 6.
- **Brand art.** No logo/favicon exist. Zensical's modern-theme defaults until that changes.
