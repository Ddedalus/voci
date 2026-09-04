# Docs site plan

Internal working document. Plans a narrative-docs-plus-reference site for velox, structured and
themed like [FastAPI's docs](../oss/fastapi/docs/en) (cloned at `oss/fastapi/` for reference). 

## Structure

- **Tutorial** (`tutorial/`) — ordered, narrative, one concept per page, each built around a
  runnable code sample.
- **How-to** (`how-to/`) — short, task-focused recipes assuming the tutorial already happened.
- **Reference** (`reference/`) — [mkdocstrings](https://mkdocstrings.github.io/)-generated API
  pages, one per public symbol group, rendered straight from docstrings.
- **Migrating from pytest** - dedicated documentation on migrating existing test suites. Covers usage of velox-migrate and common manual edit patterns.
- **About** (`about/`, plus `index.md`) — what it is, why, and orientation.

The generator is [Zensical](https://zensical.org/) with **modern** theme and mkdocstrings.

Zensical is alpha software. None of the gaps found so far touch what this plan needs.

The site lives in `docs/`.

```
zensical.toml                     # must be in repo root
docs/
  index.md                        home page, adapted from README.md
  guide/
  how-to/
  migrating-pytest/
  reference/
  about/
  img/  css/  js/
```

`zensical.toml` lives at the repo root as Zensical's docs_dir currently cannot be set to `.`.

## Navigation

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
  about/alternatives.md    the one neutral pytest comparison from README.md, not expanded
                            ("Don't argue with pytest" — velox-docs skill)
```

Every guide/how-to page must pass the velox-docs skill's rules.

## Code samples: reuse `examples/`

Plan: guide pages carry short, hand-written inline snippets (a fixture, a test — a few lines), and each page links out to the `examples/` suite that demonstrates the fully worked version.

Trade-off accepted: inline snippets aren't independently test-run.

Embedding mechanism: `pymdownx.snippets` to pull a marked block out of an `examples/` file.

## Reference pages: mkdocstrings

Same mechanism as FastAPI: `mkdocstrings[python]`, `::: velox.fixture`-style directives, reading docstrings that already follow the velox-docs register. `filters: ['!^_']` to keep private names out, `show_root_heading`, `merge_init_into_class`, `signature_crossrefs` — 

Configured under `[project.plugins.mkdocstrings.handlers.python.options]` in `zensical.toml`, per
[Zensical's mkdocstrings docs](https://zensical.org/docs/setup/extensions/mkdocstrings/). velox's
docstrings use the `:param:` field syntax, so the handler reads them as `docstring_style = "sphinx"`.

`reference/cli.md` is the one page mkdocstrings can't produce — argparse has no docstring-driven
autodoc path. Generate it the way `velox/_assertions/_vendor/` is generated: a script
(`scripts/gen_cli_reference.py`) that imports `velox.cli.build_parser()` and renders its help text into the page, plus a `--check` mode wired into `just docs check.

## Tooling

- `pyproject.toml`: new `[dependency-groups] docs` — `zensical`, `mkdocstrings[python]`
- `just docs` module

## Phasing

1. ~~**Skeleton**~~ — **done.**: just recipes, config file, docs confirmed serving locally.
2. ~~**Reference**~~ — **done.**: mkdocstrings wired up, the five symbol-group pages,
   `reference/cli.md` plus its generator script and `just docs check`.
3. **Guide** — the 14 pages above, each with its inline snippet and a link into the matching
   `examples/` suite.
4. ~~**How-to**~~ — **done.**: the four recipe pages, each derived from a specific `examples/`
   file.
5. **About** — `index.md`, `alternatives.md`. `rationale.md` is an internal document under
   `plans/`, so a public "why" page is written fresh rather than linked.
6. **CI** — `docs-check` into `just check`; hosting (GitHub Pages or otherwise) is a follow-up decision once the repo is public — out of scope here.

Each phase is a reviewable unit on its own branch, per the worktree workflow.

## Open questions

- **Hosting.** Not decided here — the repo isn't public yet, so there's nowhere to point a
  `site_url`/`repo_url` at. Revisit at phase 6.
