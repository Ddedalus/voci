# voci-migrate: findings from a second high-effort correctness review

A follow-up `/code-review high` pass over the whole `voci-migrate/` package as it stands on
`main` (not scoped to a single diff, unlike migrate-review-followups.md's pass over the
dependency-typing merge). Eight review angles ran; seven came back clean — `cli.py`,
`workspace.py`, and `matrix.py` in particular were traced independently by a second angle and
turned up nothing beyond what's below. `just check` (1013 tests) passed throughout.

The one finding — `_grouped` fusing two stacked hook-only axes into one, the sibling gap
migrate-review-followups.md's hook+mark fix didn't reach — is fixed; see `_grouped`/`_hook_grouped`
in `convert/parametrize.py` and `test_two_stacked_hook_axes_stay_two_axes` /
`test_a_composite_hook_axis_with_a_repeated_column_stays_one_axis` in
`tests/test_convert_parametrize.py`.

## References

See [migrate-review-followups.md](migrate-review-followups.md) for the prior high-effort review
pass (dependency-typing merge, `25b6561`) and its history of the same `_grouped`/`_direct_names`
machinery — including the hook+mark fix this finding's sibling gap sat next to. See
[migration-findings.md](migration-findings.md) for what real-suite runs have and haven't exercised
of this code path so far (no corpus suite yet stacks two hook-built axes).
