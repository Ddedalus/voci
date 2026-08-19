# Docs site plan

Internal working document. Plans a narrative-docs-plus-reference site for velox, structured and
themed like [FastAPI's docs](../oss/fastapi/docs/en) (cloned at `oss/fastapi/` for reference). 

## What "same look and feel" means

FastAPI's docs are [mkdocs](https://www.mkdocs.org/) +
[mkdocs-material](https://squidfunk.github.io/mkdocs-material/), with content split into four
kinds of page:

- **Tutorial** (`tutorial/`) — ordered, narrative, one concept per page, each built around a
  runnable code sample.
- **How-to** (`how-to/`) — short, task-focused recipes assuming the tutorial already happened.
- **Reference** (`reference/`) — [mkdocstrings](https://mkdocstrings.github.io/)-generated API
  pages, one per public symbol group, rendered straight from docstrings.
- **About** (`about/`, plus `index.md`) — what it is, why, and orientation.

Adopting this for velox means: mkdocs-material as the site generator and theme, the same four-way
split, and mkdocstrings for the reference section so it stays truthful to the source instead of
hand-duplicated.

## Where the site lives

Internal working documents (this file included) live in `plans/`, not `docs/`.

```
docs/
  mkdocs.yml
  rationale.md                    existing, unchanged — canonical WHY doc, cited by README/CLAUDE.md
  index.md                        home page, adapted from README.md
  guide/                          = FastAPI's tutorial/
  how-to/
  reference/
  about/
  img/  css/  js/
```

`docs_dir: .` in `mkdocs.yml` (relative to the config file, which sits in `docs/`) points mkdocs
straight at `docs/`and no `docs/en/`
locale layer either.

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

Embedding mechanism: `pymdownx.snippets` (ships with `pymdown-extensions`, a mkdocs-material
dependency already) to pull a marked block out of an `examples/` file into a fenced code block.

## Reference pages: mkdocstrings

Same mechanism as FastAPI: `mkdocstrings[python]`, `::: velox.fixture`-style directives, reading
docstrings that already follow the velox-docs register. `filters: ['!^_']` to keep private names
out, `show_root_heading`, `merge_init_into_class`, `signature_crossrefs` — the same options
FastAPI sets, since they're generic mkdocstrings behavior, not FastAPI-specific.

`reference/cli.md` is the one page mkdocstrings can't produce — argparse has no docstring-driven
autodoc path. Generate it the way `velox/_assertions/_vendor/` is generated: a script
(`scripts/gen_cli_reference.py`) that imports `velox.cli.build_parser()` and renders its help text
into the page, plus a `--check` mode wired into `just docs-check` the same way `just vendor-check`
guards the vendored tree, so the reference can't drift from the real flags.

## Theme

`mkdocs-material`, `custom_dir` skipped for v1 (no logo/favicon assets exist yet — placeholder
Material icons are fine until there's real brand art). Carry over the parts of FastAPI's `theme:`
block that are generic Material features, not FastAPI branding or i18n plumbing:
`content.code.copy`, `content.code.annotate`, `navigation.tabs`, `navigation.footer`,
`navigation.top`, `toc.follow`, `search.highlight`/`search.suggest`, light/dark palette toggle.
Drop: `alternate:` (translation switcher), the social-icon row (no public accounts yet), `logo`/
`favicon` (no assets yet — flag as a follow-up once there's brand art).

## Tooling

- `pyproject.toml`: new `[dependency-groups] docs` — `mkdocs`, `mkdocs-material`,
  `mkdocstrings[python]`.
- `justfile`: `docs-serve` (`mkdocs serve -f docs/mkdocs.yml`), `docs-build` (`mkdocs build
  -f docs/mkdocs.yml --strict`), `docs-check` (`gen_cli_reference.py --check`, then
  `docs-build`) — same shape as the existing `vendor`/`vendor-check` pair.
- `--strict` makes a broken internal link or an unresolved `nav` entry fail the build, standing in
  for the doc tests FastAPI's own CI runs.

## Phasing

1. **Skeleton** — `mkdocs.yml`, theme config, `index.md` adapted from `README.md`, placeholder
   `index.md` per section, `docs-serve`/`docs-build` recipes, `docs` dependency group. Confirms
   the site builds and looks right before content is written.
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
- **Brand art.** No logo/favicon exist. Placeholder Material icons until that changes.
