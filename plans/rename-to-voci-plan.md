# Rename velox → voci

`velox` is taken on PyPI (the project currently publishes as `velox-test` to work around it).
`voci` and `voci-migrate` are both free — confirmed 2026-09-04 via `pypi.org/pypi/<name>/json`
(404 on both). This plan is the full rename: source, config, docs, generated artifacts, GitHub,
PyPI.

Scope, so the size of this is not a surprise going in: ~2,660 occurrences of `velox` across ~250
files in `velox/`, `velox-migrate/`, `tests/`, `docs/`, `spec/`, `plans/`, `examples/`, root
config, and `.claude/skills/`. This is a mechanical sweep, not hundreds of individual decisions —
one naming map, applied everywhere, plus a handful of named exceptions below.

## Work to do

### 1. Naming map (decisions, locked)

Plain case-preserving substring replacement of `velox` → `voci`, `Velox` → `Voci`,
`VELOX` → `VOCI`, everywhere in-scope. This is deliberately a substring rule, not a
word-bounded one: it has to reach into `velox_migrate`, `velox-migrate`, `.velox_cache`,
`VELOX_REWRITE_CACHE`, `velox.fixture` in one pass and produce `voci_migrate`, `voci-migrate`,
`.voci_cache`, `VOCI_REWRITE_CACHE`, `voci.fixture` correctly.

Two named exceptions the mechanical rule gets wrong, both applied by hand, not by the substring
script:

- **`velox-test` (the PyPI distribution name) becomes `voci`, not `voci-test`.** The `-test`
  suffix existed only to dodge the `velox` collision; `voci` doesn't have that collision, so the
  suffix drops. Everywhere `velox-test` appears (root `pyproject.toml` `[project].name`,
  `README.md`'s install line, `examples/01-fastapi-crud`'s dependency + `[tool.uv.sources]`,
  `release.yml`'s PyPI environment URL) gets this fix.
- **The migration tool's `VX###` construct codes (105 of them, e.g. `VX214`) become `VC###`**
  ("Voci Construct", same shape as today's unstated "Velox Construct"). These don't spell out
  `velox`, so the substring rule never touches them — needs its own pass, a regex like
  `\bVX(\d{3})\b` → `VC\1`, applied case-sensitively (never lowercase `vx`) across
  `velox_migrate/matrix.py` (the 105 definitions), `velox_migrate/{audit,convert,report}/*.py`,
  `velox-migrate/tests/*.py`, `docs/migrate/matrix.md`, `docs/migrate/index.md`,
  `plans/migration-findings.md`, `plans/trio-support-plan.md`. Not present in
  `velox-migrate/corpus/` (checked — no dump or showcase file bakes in a VX code), so no corpus
  regeneration needed for this one. `VELOX-TODO[category]` (the marker converted source actually
  carries) is a different string — plain `VELOX`, caught by the ordinary substring rule, becomes
  `VOCI-TODO[category]` for free.

Everything else is the mechanical rule applied consistently:

| Old | New |
|---|---|
| `velox/` (package dir) | `voci/` |
| `velox-migrate/` (dir), dist name, CLI command | `voci-migrate/` |
| `velox_migrate` (import package) | `voci_migrate` |
| `velox` CLI command, `import velox`, `@velox.fixture` etc. | `voci` |
| `[tool.velox]` config table | `[tool.voci]` |
| `.velox_cache`, `.velox-migrate` work dir | `.voci_cache`, `.voci-migrate` |
| `VELOX_REWRITE_CACHE` env var | `VOCI_REWRITE_CACHE` |
| `VELOX_REWRITER_REVISION`, `VELOX_CODEGEN_OPTIONS`, `VELOX_BLOCK`, `VELOX_REPORT_VERSION`, `_VELOX_DIR` (internal constants) | `VOCI_*` equivalents |
| `velox-example-*` (example project names) | `voci-example-*` |
| GitHub `Ddedalus/velox` | `Ddedalus/voci` |
| `VX###` construct codes (§1 exception, own regex) | `VC###` |

Not touched: `oss/*` (pinned third-party submodules — FastAPI, pytest, httpx, etc.; editing
checked-out submodule content is out of scope and would dirty their pinned commits). `uv.lock`
regenerates rather than being hand-edited. `/site` and `/.cache` are gitignored build output —
delete and let `just docs build` regenerate rather than editing.

### 2. Do the sweep in a worktree

Large mechanical refactor touching nearly every file → worktree workflow, not direct-commit
(`.claude/skills/dev-workflow/SKILL.md`).

- `git worktree add ../velox-wt-rename-voci -b rename-voci`, `EnterWorktree`, `just sync` inside it.
- `git mv velox voci`
- `git mv velox-migrate voci-migrate && git mv voci-migrate/velox_migrate voci-migrate/voci_migrate`
- `git mv .claude/skills/velox-docs .claude/skills/voci-docs`
- Run the substring sweep (script, not by hand) over every tracked file except `oss/`, `.git/`,
  `.venv*/`, `site/`, `.cache/`, `.ruff_cache/`, `.pytest_cache/`, `uv.lock` — a `git ls-files`
  walk with the three-case substitution is enough; ripgrep/sed works too if it preserves case.
- Apply the `velox-test` → `voci` exception by hand at its four call sites (§1).
- Apply the `VX###` → `VC###` regex pass (§1) — separately from the substring sweep, since it
  doesn't spell `velox` and the sweep won't reach it.
- Fix up path-shaped strings the substring rule won't reach on its own: `[tool.hatch.build.hooks.vcs] version-file`, `[tool.hatch.build.targets.wheel] packages`, `pyrefly` `project-includes`/`project-excludes`/`search-path`, `ruff` `extend-exclude`, `isort` `known-first-party`, `justfile`/`recipes/*.just` path references, CI workflow paths (`velox-migrate/velox_migrate` → `voci-migrate/voci_migrate` etc.) — these are directory paths, so `git mv` above already renamed the targets; this step is confirming every reference to those paths was swept too, since a path is textually just `velox...` and the substring rule should already have caught it. Spot-check rather than assume.
- Root `CLAUDE.md`: update the `velox-docs` skill reference to `voci-docs`.

### 3. Regenerate generated artifacts (don't hand-edit these)

- `just vendor update` — re-vendors `voci/_assertions/_vendor` from the `oss/pytest` submodule
  using the now-renamed `scripts/vendor_assertion.py` (which embeds `VOCI_REWRITER_REVISION` etc.
  into the output). Hand-editing the vendor tree instead would defeat the point of it staying a
  cheap diff against upstream.
- `just migrate corpus-dumps` — the corpus fixture sources (e.g.
  `typed_showcase/test_top.py::test_a_builtin_is_typed_by_velox`) get swept like any other file;
  the checked-in ground-truth JSON dumps that reference those names need regenerating, not editing.
- `just docs cli` and `just docs matrix` — regenerate `docs/reference/cli.md` and
  `docs/migrate/matrix.md`'s generated blocks from the renamed `voci.cli`/`voci_migrate.matrix`
  rather than hand-editing the generated markers.
- `rm -rf .venv .venv-3.13 && just sync && just py sync 3.13` — both venvs have editable installs
  keyed to the old dist/package names; rebuild rather than trust `uv sync` to patch them in place.
- `uv lock` (or let the above `sync` do it) to regenerate `uv.lock` under the new names.

### 4. Verify

- `just check` (default interpreter: lint, fmt-check, typecheck, test)
- `just checks test-all` (both suites, both interpreters — this is a version-branch-adjacent
  change in the sense that it touches every file `sys.version_info` branches live in, even though
  it isn't one)
- `just migrate test`, `just migrate corpus-check`
- `just docs check` (site builds clean, generated pages match sources)
- `just vendor check`
- `git grep -i velox` over the worktree should come back empty except inside `oss/` and, if kept
  around as a compatibility note, this plan file's own history

### 5. Review and merge

`/code-review high rename-voci` given the size — a mechanical sweep still has real ways to go
wrong (a missed `velox-test` special-case, a docstring that quoted `velox` as prose about the old
name rather than as an identifier, an env var some CI secret still expects under the old spelling).
Address findings, merge to `main`, delete the worktree per the workflow's cleanup steps.

### 6. GitHub

- `gh repo rename voci` from the `main` checkout (equivalent: repo Settings → rename). GitHub
  auto-redirects the old URL and existing clones' `origin` remotes keep working, so this is safe
  to do whenever — no hard ordering dependency on the code sweep, but doing it after merge keeps
  the rename atomic from an outside observer's perspective (one moment the project is `velox`,
  the next it's `voci`, not a straddling state).
- After renaming, `git remote set-url origin https://github.com/Ddedalus/voci.git` in this
  checkout (and any other local clones/worktrees) — the redirect works but pins to it forever
  otherwise.
- That's the whole GitHub side. No CODEOWNERS, branch-protection rules, or webhook config in this
  repo references the name.

### 7. PyPI

Not a rename — `voci` is a brand-new PyPI project, since PyPI has no rename operation and the
distribution name is changing (`velox-test` → `voci`, not `velox-test` → `voci-test`).

- Nothing to publish yet if `velox-test` was never actually released (no git tags, no
  `gh release list` output as of this plan — check again before assuming, in case a release
  happened outside this checkout's view).
- When the first release does happen: register PyPI trusted publishing (OIDC) for the new `voci`
  project from scratch — owner `Ddedalus`, repo `voci`, workflow `release.yml`, environment
  `pypi`. `release.yml`'s existing `environment.url` already points at `pypi.org/project/voci/`
  after §1/§2's sweep + exception; the trusted-publisher registration is a PyPI-side setting that
  doesn't exist until someone creates it there, unrelated to anything in this repo.
- `velox-migrate` has no PyPI publish job today (`release.yml` only builds/publishes the root
  package) — nothing to register for `voci-migrate` until that changes.
- The old `velox-test` PyPI project, if anything was ever pushed to it, is simply abandoned; PyPI
  doesn't support deleting or redirecting a project, so there's no cleanup action beyond deciding
  whether to yank any releases on it (only relevant if it was ever actually published to).

### 8. Outside the repo (not part of the worktree's diff, do separately/manually)

- This checkout's own directory is named `velox` on disk (`/home/hubert/velox`) — cosmetic only,
  renaming it is optional and yours to do (`mv`, then point any shell aliases/IDE workspaces at
  the new path).
- `~/velox-headless` — the permanent headless worktree noted in memory
  ([project-headless-worktree](/home/hubert/.claude/projects/-home-hubert-velox/memory/project-headless-worktree.md)).
  Never remove it per that memory; renaming or repointing it is a separate decision, not implied
  by this plan.
- Update Claude memory afterward: `project-velox.md`'s architecture notes, the worktree-typecheck
  memory's `velox-wt-<name>` convention example, and `MEMORY.md`'s index all still say `velox` —
  worth a pass once the rename has actually landed, not before (memories should describe what's
  true, and it isn't yet).

## References

- `plans/CLAUDE.md` — plan-file conventions this file follows.
- `.claude/skills/dev-workflow/SKILL.md` — worktree workflow used for §2–5.
