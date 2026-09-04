---
name: user-output
description: top-level agent must load this prior to sending final answer to user
---

When answering in the chat:

# Output format

Provide a human-readable, structured summary of the _results_ of your session.

Main things are, in this order:
 * Next step according to plan, if applicable
 * Risk / uncertainty in implementatoin: ideally low
 * Material deviation from plan (plan gaps need not be mentioned)
 * Any failure of the dev environment
 * List unrelated problems you discovered

Adjust the level of detail to the complexity of the task. Assume the user remembers their ask and has skimmed an pre-existing plan.

## What I don't need
 * Detailed explanations of what you did. I don't care and if I did, I'd check git.
 * References to individual file changes
 * Notes that some minor things were not in the original plan
 * Prose descriptions of just checks you run.
 * Listing of review findings that you've patched on the way
 * References to your subagents
 * Counterfactuals
 * Quotes or summary from plan you created or updated
 
## Example response: minor task into main

> Tests passed, committed. Risk is very low.

## Example response: plan phase

> ## Status
> Work is merged. Risk: low.
> ## Next step
> Phase 4: coverage implementation <link to plan>
> ## Problems detected
> Linter fails in worktree that is git-ignored. I run it on the main checkout and added a fix request to ROADMAP.md.

That is it, it's really enough. Only discuss details that actually require me to change course.

## Unrelated problems

Generally you're autonomous and you will have to solve any problem in this workspace eventually. The only question is: now or later.

1. If the problem is blocking your task, spawn a sub-agent to solve it and merge into main, following usual protocol, but make sure to stay in the same worktree.
2. If the problem is not blocking but vaguely related to your task, solve it if you have capacity, with a subagent.
3. If the problem is not blocking and not related to your task, add an entry in ROADMAP.md and reference it in final answer.
