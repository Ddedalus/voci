# voci-migrate: findings from a second high-effort correctness review

A follow-up `/code-review high` pass over the whole `voci-migrate/` package as it stands on
`main` (not scoped to a single diff, unlike migrate-review-followups.md's pass over the
dependency-typing merge). Eight review angles ran; seven came back clean — `cli.py`,
`workspace.py`, and `matrix.py` in particular were traced independently by a second angle and
turned up nothing beyond what's below. `just check` (1013 tests) passed throughout.

## Work to do

- [ ] **`_grouped` still merges two stacked hook-only axes into one** (VC031's blind spot).
  `convert/parametrize.py`'s `_grouped` (around
  [parametrize.py:551](../voci-migrate/voci_migrate/convert/parametrize.py#L551)) disambiguates
  two stacked "direct" axes by their `@pytest.mark.parametrize` position via `_mark_positions`,
  falling back to raw `CallSpec.indices` only for a name no mark covers. migrate-review-followups.md
  already closed the case where one such hook-built axis stacks with a *marked* one
  (`test_a_hook_axis_stacked_with_a_mark_stays_its_own_axis`); this is the sibling gap the fix
  didn't reach — two axes *both* built by a `pytest_generate_tests` hook, with no mark on either.
  Neither has a `_mark_positions` entry, so both key on
  `tuple(spec.indices.get(name, -1) for spec in specs)`, and pytest's own
  `Metafunc._recompute_direct_params_indices` folds that signature identically across every
  "direct" param once two or more stack on a test — hook-built axes included, despite the
  function's docstring assuming otherwise. `_grouped` merges them into one group, `axes()` never
  calls `_direct_names`/`_row_index` to split it back apart, `_position_of` can't attribute either
  axis's real per-call id, and `_read_generated` silently writes one fused
  `@voci.parametrize(('x', 'y'), [...])` instead of two independent ones — with no `VC031` finding
  raised to flag the loss. No existing test covers hook+hook stacking (only hook+mark). Fix
  shape: extend whatever ground truth `_direct_names`/`_row_index` already reconstruct for the
  marked case to also cover a hook-only pair, keyed off `ground_truth.items`'s dump the same way
  `_direct_names`'s docstring describes for the marked axis.

## References

See [migrate-review-followups.md](migrate-review-followups.md) for the prior high-effort review
pass (dependency-typing merge, `25b6561`) and its history of the same `_grouped`/`_direct_names`
machinery — including the hook+mark fix this finding's sibling gap sits next to. See
[migration-findings.md](migration-findings.md) for what real-suite runs have and haven't exercised
of this code path so far (no corpus suite yet stacks two hook-built axes).
