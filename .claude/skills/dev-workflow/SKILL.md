---
name: dev-workflow
description: Session workflow rules for velox — how to choose between direct-commit, worktree, and branch workflows for a task, and the exact worktree setup/cleanup steps (sibling directory placement, sync, code review, merge). Load before starting a non-trivial task (feature, refactor, roadmap item, or "work in a branch") to decide how to structure the session.
---

# velox dev workflow

This is a prototype repo not yet published. We're working alone locally: there is no human code
review, just fast AI iteration. Pick the workflow below for the task at hand.

## Direct commit workflow

When asked to work on docs, roadmap, or other admin/plans, commit directly to the current branch,
including main. Also use this for CI or test fixes where the job is reasonably small.

## Worktree workflow

When asked to do work on a feature or larger refactor:
1. Create the worktree as a sibling of the repo, not with `EnterWorktree`'s default
   `.claude/worktrees/` placement: `git worktree add ../velox-wt-<name> -b <branch>`, then
   `EnterWorktree(path: "/home/hubert/velox-wt-<name>")` to attach the session to it. Reason:
   `.git/info/exclude` hides `**/.claude/worktrees/` from git status, and pyrefly honors that same
   file when resolving `project-includes` globs, so `just checks typecheck`'s first command finds
   zero files and fails for any worktree placed there. A sibling directory doesn't match that
   pattern. Run `just sync` once inside the new worktree before `just check` — it has its own
   `.venv`.
2. Do the work. Commit.
3. Spawn /code-review <level> <branch> (default: medium, hard for very complex changes)
4. Address all findings. Commit.
5. Clean up docs, roadmap, etc.
6. Merge into main. Delete the worktree — since it was entered via `EnterWorktree(path:...)`
   rather than created by it, `ExitWorktree(action: "remove")` will refuse; instead
   `ExitWorktree(action: "keep")` then `git worktree remove ../velox-wt-<name>` and
   `git branch -D <branch>` from the main checkout.

If **material** uncertainty exists after plan or implementation, explain and only merge once
clarified. When asked to implement a roadmap item, delete its entry from ROADMAP.md as part of the
worktree.

## Branch workflow

Only branch in the current worktree if explicitly asked. Do not merge until instructed.
