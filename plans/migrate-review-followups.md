# velox-migrate: findings from the high-effort correctness review

Eight-angle `/code-review high` of the dependency-typing merge (`25b6561`), scoped to
`velox-migrate/` and read for data loss or incorrect conversion. Two confirmed correctness bugs
were fixed directly at the time, each with a regression test. The reuse/simplification/efficiency/
altitude cleanups the same review surfaced were fixed in a later pass (dead `is not None` guards
removed now that every real call site always supplies the value, `CLEAN_EXIT_STATUSES` and JSON- and
Markdown-table writing de-duplicated, `Resolver`'s per-injection reparse and `readiness`'s
double graph-walk memoized/shared, `verify.run()`'s two runners parallelized, and the duplicated
`_SYNTHETIC_PREFIXES` constant shared) — see the code and its tests rather than this document for
the detail.

What's left is the lower-confidence correctness gaps, which need a decision before a fix, and two
altitude notes not worth a change on their own.

## Correctness — lower confidence, needs a closer look

**`_takes()` misses `request` forwarded into a plain helper, or under a customized
`python_functions`.**
[audit/sources.py](../velox-migrate/velox_migrate/audit/sources.py) `_takes()` only treats a
`request` parameter as pytest's own when the innermost declaring frame `injects` — a fixture
factory or a function whose own name starts with `test`. Two real misses follow: a helper function
that a test hands its `request` fixture into (`def _register_cleanup(request): ...`) is not
recognized, so a hazardous `request.addfinalizer`/`.node`/`.getfixturevalue` use inside it goes
unreported; and a suite with `python_functions = ["*_check"]` in its ini has none of its real test
functions recognized either, since none start with `test`. The narrowing itself is deliberate and
partly tested (`test_a_request_parameter_of_a_function_pytest_never_calls_is_not_the_fixture`), but
neither miss has a regression test, and the `python_functions` case isn't mentioned by VX302 in a
way that stops the hazard scan from running on the wrong assumption. Needs a decision: read
`ground_truth.items`' actual collected names instead of a `"test"` prefix check, and/or track
`request`-forwarding into a helper explicitly.

**`_absolute()`'s relative-import level arithmetic may be off by one at the rootdir boundary.**
[convert/annotate.py](../velox-migrate/velox_migrate/convert/annotate.py) resolves a relative
import's absolute dotted path from `node.level` and the file's parent parts:
`node.level - 1 > len(parts)` guards against going past the rootdir. For a file two packages deep
and a level-3 import, this evaluates `2 > 2 = False` — not rejected — which may or may not match
the level-2 case's boundary. Flagged with low confidence; needs concrete fixtures (a `level=3`
import from a two-deep file) run through `_absolute()` to confirm whether it over- or
under-resolves, since a wrong answer here silently drops a `TYPE_CHECKING` import an injected
parameter's annotation depends on.

## Altitude — not urgent, revisit before a second plugin of the kind

- `wiring.py`'s `_backend_findings`/`_backend_name` (VX324) key detection of "parametrized onto a
  non-asyncio backend" off exactly the `anyio_backend` callspec parameter and the literal string
  `"asyncio"` — a different async plugin exposing an equivalently-purposed fixture under another
  name produces no finding.
- `audit/sources.py`'s injection heuristic (`name.startswith("test")`, described above) never
  checks the enclosing class against `Test*`/`python_classes` either, so a plain helper class with
  a `test_*`-named diagnostic method is treated as a pytest test method.

Neither is urgent — each degrades an audit finding's precision, not conversion output.

---

See [ROADMAP.md](../ROADMAP.md) § Code quality consolidation.
