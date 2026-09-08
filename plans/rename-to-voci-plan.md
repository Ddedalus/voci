# Rename velox → voci

Package renamed source-to-docs on branch `rename-voci`, plus generated artifacts regenerated and
every check green. What's left is outside this repo's diff: the GitHub repo itself, and PyPI.

## Done (2026-09-08)

Naming map, worktree sweep, generated-artifact regeneration, full verification (§4's whole
CI-mirroring + local-only list), and the marshmallow smoke test (§5) — all completed on branch
`rename-voci`. Numbers matched the recorded baseline in `plans/migration-findings.md` exactly:
pytest 1188 passed; `voci --serial` and `voci` (9.5x) both 1183 passed / 5 failed, same five
`test_registry.py` cases. Merged to `main`.

The smoke test's re-run surfaced two pre-existing bugs, confirmed present on `main` *before* the
rename too (not a rename regression) — recorded in `plans/migration-findings.md`'s 2026-09-08
addendum: `convert --write`'s baseline recording breaks on a suite that overrides pytest's
`norecursedirs` (marshmallow does), and 15 `VOCI-TODO[VC114]` markers now appear where the
original run had none. Neither was fixed here — out of scope for a rename.

## Work still to do

### 7. GitHub

- `gh repo rename voci` from the `main` checkout (equivalent: repo Settings → rename). GitHub
  auto-redirects the old URL and existing clones' `origin` remotes keep working, so this is safe
  to do whenever.
- After renaming, `git remote set-url origin https://github.com/Ddedalus/voci.git` in this
  checkout (and any other local clones/worktrees) — the redirect works but pins to it forever
  otherwise.
- That's the whole GitHub side. No CODEOWNERS, branch-protection rules, or webhook config in this
  repo references the name.

### 8. PyPI

Not a rename — `voci` is a brand-new PyPI project, since PyPI has no rename operation and the
distribution name is changing (`velox-test` → `voci`, not `velox-test` → `voci-test`).

Done (2026-09-08): trusted publishing registered for `voci` (owner `Ddedalus`, repo `voci`,
workflow `release.yml`, environment `pypi`); `release.yml` now also builds/publishes
`voci-migrate` on the same `vX.Y.Z` tag/Release, through a second environment, `pypi-migrate` —
PyPI ties each (repo, workflow, environment) triple to one project, so the two packages need
separate environments even sharing one workflow file and tag. One tag rather than two per-package
tags: they're tested and released together, and a second GitHub Release object per version was
judged more confusing than useful. `voci-migrate/pyproject.toml` gained hatch-vcs dynamic
versioning off that same tag to match.

Still open: register `pypi-migrate` as a trusted publisher for the `voci-migrate` project on PyPI
(same repo/workflow, environment `pypi-migrate`) and create that environment in the GitHub repo's
settings, with the same `v*` tag pattern as `pypi` — both are PyPI/GitHub-side settings, not part
of this repo's diff. First actual release (tag `vX.Y.Z` + GitHub Release, publishing both
packages) still pending too.

`velox-test` was never pushed to PyPI, so there's nothing to clean up or yank there.

### 9. Outside the repo (not part of the merged diff, do separately/manually)

- This checkout's own directory is still named `velox` on disk (`/home/hubert/velox`) — cosmetic
  only, renaming it is optional and yours to do (`mv`, then point any shell aliases/IDE workspaces
  at the new path).
- `~/velox-headless` — the permanent headless worktree noted in memory
  ([project-headless-worktree](/home/hubert/.claude/projects/-home-hubert-velox/memory/project-headless-worktree.md)).
  Never remove it per that memory; renaming or repointing it is a separate decision, not implied
  by this plan.
- Claude memory still says `velox` throughout: `project-velox.md`'s architecture notes, the
  worktree-typecheck memory's `velox-wt-<name>` convention example, and `MEMORY.md`'s index. Worth
  a pass now that the rename has landed — but only once §7/§8 (or a decision to skip them) settles
  what's actually true, so the memory update happens once rather than twice.

## References

- `plans/CLAUDE.md` — plan-file conventions this file follows.
- `.claude/skills/dev-workflow/SKILL.md` — worktree workflow used for the completed sweep.
- `plans/migration-findings.md` — the original marshmallow run the smoke test repeated, with its
  numbers and the 2026-09-08 re-run addendum.
