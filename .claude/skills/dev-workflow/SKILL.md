---
name: dev-workflow
description: top-level agent must always load this to pick commit workflow
---

# voci dev workflow

We're working alone locally: there is no human code review, just fast AI iteration. Pick the workflow below for the task at hand.

## Layout

The project lives at `~/voci/` with four permanent sibling worktrees of the same clone:
`~/voci/main` (the human's own checkout — agents don't commit here unless told to), and
`~/voci/wt1`, `~/voci/wt2`, `~/voci/wt3` (agent workspaces). A separate, local-only,
`main`-only git repo at `~/voci/plan` tracks which slot is doing what — see its own README. It
exists so an agent in `wt2` can see what `wt1` claimed without waiting on a branch merge.

If you're a subagent reading this because your dispatcher told you which slot to use, skip
straight to step 2 of whichever workflow applies — slot claiming is the dispatcher's job.

## Direct commit workflow

When asked to work on docs, roadmap, or other admin/plans, commit directly to the current branch,
including main. Also use this for CI or test fixes where the job is reasonably small.

## Slot workflow

When asked to do work on a feature or larger refactor:
1. Claim a slot: read `~/voci/plan/worktrees/*.md`, pick one with `status: idle` (never `main`
   unless told to). Get your own Claude session id by matching your `cwd` in
   `claude agents --json` (once you've `cd`ed into the slot). Update that slot's file —
   `status: active`, `plan` (the code-repo plan slug, if any), `branch`, `session_id`,
   `last_heartbeat: <now, UTC>` — and `git add -A && git commit` in `~/voci/plan`. If all three
   agent slots are busy, fall back to the Ad hoc worktree workflow below instead of waiting.
2. In the slot: `git checkout main && git pull && git checkout -b <branch>`. Run `just sync` if
   `uv.lock` moved since you last worked here — each slot has its own `.venv`. Do the work,
   committing as you go. Bump `last_heartbeat` in your slot file on any long-running step.
3. Spawn /code-review <level> <branch> (default: medium, hard for very complex changes)
4. Address all findings. Commit.
5. Clean up docs, roadmap, etc.
6. Merge into main. Release the slot: `git checkout main && git branch -D <branch>` in the
   worktree, then set the slot file back to `status: idle`, clear `plan`/`session_id`, commit in
   `~/voci/plan`.

If **material** uncertainty exists after plan or implementation, explain and only merge once
clarified — or, if the plan lives in `~/voci/plan/plans/<slug>.md`, record the question there
under `status: needs-input` and stop; that's what the janitor's notifier watches for. When asked
to implement a roadmap item, delete its entry from the plan repo's `ROADMAP.md`
(`../plan/ROADMAP.md`) as part of the slot's work.

## Ad hoc worktree workflow

Fallback for one-off work outside the four permanent slots (all busy, or a throwaway spike):
1. `git worktree add ../voci-wt-<name> -b <branch>` from `~/voci/main`, sibling to the repo —
   not `EnterWorktree`'s default `.claude/worktrees/` placement. Reason: `.git/info/exclude` hides
   `**/.claude/worktrees/` from git status, and pyrefly honors that same file when resolving
   `project-includes` globs, so `just checks typecheck`'s first command finds zero files and fails
   for any worktree placed there. Then `EnterWorktree(path: "/home/hubert/voci-wt-<name>")` to
   attach the session to it, and `just sync` inside it before `just check`.
2. Do the work, review, merge — same as Slot workflow steps 2-5.
3. Merge into main. Delete the worktree — since it was entered via `EnterWorktree(path:...)`
   rather than created by it, `ExitWorktree(action: "remove")` will refuse; instead
   `ExitWorktree(action: "keep")` then `git worktree remove ../voci-wt-<name>` and
   `git branch -D <branch>` from the main checkout.

> WARNING: EnterWorktree requires user approval. You must call this ASAP in the session, before
> the user goes away. Do this before planning and code exploration. The Slot workflow above avoids
> this entirely — prefer it whenever a slot is free, including for headless/background sessions.

## Branch workflow

Only branch in the current worktree if explicitly asked. Do not merge until instructed.
