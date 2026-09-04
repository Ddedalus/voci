# plans

Internal working documents — research syntheses and implementation plans, written for whoever is
building the thing rather than for a user. Milestone vocabulary, spec citations, and "internal
working document" framing belong here, not in `docs/`. Files here cross-reference each other, so a
link to another file in `plans/` stays a bare filename, not a `plans/`-prefixed path.

## Plan structure

Generally in this order:
 * Work done - very brief pointers
 * Work to do - top of file, tickboxed entries or markdown headers, depending on size
 * User input and decisions summary
 * Description of the thing we're building
 * References and other material

## Getting work done

When you complete an item in a plan, mark it as done and delete all information that is now in docs or code. Only keep a tiny record of the done item.

Also update any downstream work that needs changing due to a discovery you made, e.g. a wrong assumption.

## Sub-plans

Work that involves major complexity can delegate to a sub-plan, up to 1 level deep. Subplan may be deleted once the work is done. Must clearly link.

## Findings, audit, learning etc.

Keep this stuff out of the plan. You have rationale.md, docs/ and dedicated findings files instead. The plan is laser-focused on what to do.