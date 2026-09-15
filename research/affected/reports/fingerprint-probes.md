# Probes: qualname mapping, fingerprint coverage, module-level attribution, assertion rewriting, callback cost

> Subagent report from the 2026-09-12 research pass, kept verbatim. Paths under `/tmp` were rewritten to their copies in `research/testmon/`; the venvs they ran in weren't kept (see `../README.md`).

> **Correction (2026-09-12):** the Q8 numbers below are wrong. Re-measured with `../probes/sysmon_callback_cost.py`, a callback that records costs ~3x on a trivial function whether or not it dedups, an empty callback ~1.8x, and `DISABLE` ~1x. The plan's Tracer section has the corrected figures.

## Scripts (all under `research/testmon/fingerprint-probes/`)

`sample_mod.py`, `attrs_mod.py`, `zip_src/zmod.py`, `pyc_only/pmod.pyc`, `zippkg/lib.zip` (fixtures) — `q1_qualname.py`, `q2_duplicate_qualname.py`, `q3_fingerprint_scheme.py`, `q4_comments_whitespace.py`, `q5_module_level.py`, `q6_assertion_rewriting.py`, `q7_generated_exotic.py`, `q8_disable_dedup.py`. Run with `/home/hubert/voci/.venv/bin/python <script>.py`; version-sensitive ones also run under `/home/hubert/voci/.venv-3.13/bin/python`. `attrs` was tested via a throwaway `uv venv` at `research/testmon/fingerprint-probes/attrs-venv`.

## Q1 — Qualname correspondence

Probe: an `ast.NodeVisitor` reproducing CPython's compile-time qualname rule (function/class scope stack, `<locals>`, `<lambda>`), diffed against `co_qualname` from a PY_START tracer that imports `sample_mod.py` *inside* the monitored region (importing first, as I did on the first attempt, silently misses `<module>` and every class-body/decorator-application frame — a real footgun for an implementation).

Result: every ordinary construct matched exactly, including `Outer.Inner.InnerInner.deepest`, `closure_maker.<locals>.adder`, `<lambda>`, `has_comprehensions.<locals>.<genexpr>`, `plain_decorator.<locals>.wrapper`, async def/agen. Three categories had no AST counterpart:
- **List/set/dict comprehensions never fire PY_START at all** (3.12+ inlines them into the enclosing frame — no code object exists). Only `<genexpr>` still gets one.
- **PEP 695 generics** (`def f[T]()`, `class C[T]`) produce an extra code object with `co_qualname == '<generic parameters of generic_function>'` / `'<generic parameters of GenericClass>'` — a real frame with no matching AST node in an ordinary walker (needs special-casing `type_params`).
- **PEP 649 lazy annotations, 3.14 only.** Forcing evaluation of `func.__annotate__` fires PY_START with `co_qualname == '__annotate__'` (literally, not `has_annotation.__annotate__`) and `co_filename` equal to the real source file. On 3.13, `__annotate__` doesn't exist as a function attribute at all (`AttributeError`) — confirmed by running the same script under `.venv-3.13`.
- **`@dataclass`-generated `__init__`**: `co_filename == '<string>'`, `co_qualname == '__create_fn__.<locals>.__init__'`, `co_firstlineno == 2` — identical on 3.13 and 3.14, and identical across *every* dataclass in every module in the process (no back-reference to `Point` at all).
- **`attrs`' generated `__init__`** (tested in a separate venv with `attrs` installed): `co_filename == '<attrs generated methods attrs_mod.AttrsPoint>'`, `co_qualname == '__init__'` — a synthetic-but-*informative* filename that embeds the real module and class name, unlike dataclasses' generic `<string>`.
- A function whose only caller is a decorator that discards it (`decorator_returning_elsewhere`) never fires PY_START — expected, not a gap.

Design implication: the AST↔runtime qualname mapping holds well for everything actually written by hand, but three fully-standard, non-exotic constructs (dict/list/set comprehensions, PEP 695 generics, PEP 649 annotate functions) already break a naive 1:1 mapping, and generated `__init__`s from the two most common code-generation libraries need entirely separate handling — one traceable back to its owner via filename-string parsing (attrs), one not traceable at all (dataclasses, stdlib).

## Q2 — Duplicate qualnames

Probe: a synthetic module with `dup` defined three times (`if`/`else`/redefinition) and `stub`/`stub`/`stub` via `@overload`, traced, then compared against a line-shifted copy (`v2`, +2 blank lines +1 comment before the redefinition).

Result:
```
v1 dup() AST linenos:  [5, 8, 12]   -> runtime PY_START only ever reports firstlineno=12 (the winner)
v2 dup() AST linenos:  [7, 10, 15]  -> runtime PY_START only ever reports firstlineno=15
```
`(qualname, firstlineno)` disambiguates correctly within one source version — the overload stubs (`...` bodies) never execute, so PY_START only ever names the one function object that's actually live, and `firstlineno` picks it out from its dead siblings unambiguously.

What breaks: any edit *above* a duplicate-name def shifts every later occurrence's line number. A stored fingerprint keyed by `(qualname, old_firstlineno)` then has no counterpart in the new source at all — not a silent miss, but a forced "no match found" that must be treated as changed, over-invalidating on any edit above it (not just edits to the function itself). This is worse for files with many same-named defs (overload-heavy code) than for ordinary files, since ordinary functions have a single occurrence and can be keyed by qualname alone, falling back to `firstlineno`-based disambiguation only for genuine duplicates. A more line-shift-tolerant scheme: for duplicate qualnames, key by `(qualname, ordinal position among same-named defs in source order)` rather than raw line number — survives any edit that doesn't add/remove a same-named def.

## Q3 — What must the fingerprint cover

Probe: nine before/after source pairs, each checked against `FUNC_SRC` (hash of `ast.get_source_segment`), `FUNC_DUMP` (hash of `ast.dump`, no positions), and `MODULE_RESIDUAL` (hash of the module with every function body replaced by `pass`, decorators/signatures/defaults/imports/class-body statements kept).

| edit | FUNC_SRC/DUMP | MODULE_RESIDUAL | caught by |
|---|---|---|---|
| change decorator's own body | same | same (see below) | **neither**, see decorator-attribution finding |
| change default arg value | CHANGED | CHANGED | function |
| change dataclass field | n/a (no function exists) | CHANGED | residual only |
| change module constant read at call time | same | CHANGED | residual only |
| change import source (`from x` → `from z import y`) | same | CHANGED | residual only |
| change base class | n/a | CHANGED | residual only |
| change `__slots__` | n/a | CHANGED | residual only |
| change annotation on the def line itself | CHANGED | CHANGED | function |
| comment-only edit | CHANGED (text) / same (dump) | same (dump) / CHANGED (text) | depends on scheme |

Surprising negative result, found by extending the decorator case: a change to `deco()`'s *own body* is invisible to **both** function-level hash (target's text is untouched) **and** the module-residual as literally specified in the plan ("module source minus every function body" — that specifically excludes `deco`'s body too, since it's a function). It's only caught for whichever test's own PY_START trace happened to include `deco` — and `deco(target)` runs exactly once, at decoration time during import (proved with a two-test simulation: the importing test records `['<module>', 'deco', 'target']`, a later test in the same process that only calls `target()` records `['target']` only). This generalizes Risk #2 in the plan beyond `<module>`'s own top-level statements: **any code that runs only as a side effect of import** — decorator application, dataclass/attrs class processing, metaclass `__new__`, `__init_subclass__` — has the identical one-shot-attribution problem, and "treat module-level code as whole-file invalidation" needs to mean "treat everything that ran during this module's own import," not just the module's literal top-level statements.

## Q4 — Comments/whitespace, and cost

`ast.dump` is insensitive to comments and blank lines by construction (verified: identical hash for a function with vs. without a trailing comment, vs. with extra blank lines; source-text hash changes on both). Whether that's wanted is a judgement call the plan should make explicit, not an accident of implementation.

Timing over `oss/pytest/src` (81 files, 39k LOC):
```
ast.parse (cold, all files):                    174–292 ms
whole-file sha256 (proxy, not per-function):       ~1–2 ms
per-function ast.dump + hash (2059 funcs):      183–312 ms  (comparable to parse itself)
per-function ast.get_source_segment + hash:     913–917 ms  (3–5x MORE than dump!)
per-function fast-slice text hash (cached
  splitlines(), manual offset slicing):            65–78 ms  (cheapest of the three)
```
Surprise: I expected `ast.get_source_segment` (text) to be cheaper than `ast.dump` (AST-shape), on the theory that dumping stringifies the whole tree. Measured the opposite by ~5x: `get_source_segment` re-splits the file into lines and re-slices on *every call*, making it effectively O(functions × file_size). A correctly-implemented text hash (one `splitlines()` per file, then integer-offset slicing) is cheaper than either. So "comment-insensitive via `ast.dump`" is nearly free relative to the parse the design already pays for, and a naive `ast.get_source_segment`-based implementation — which reads as the obvious/naive choice — is actually the expensive one, not the cheap one.

## Q5 — Module-level code attribution

Part 1 (simulated in-process test runner sharing one `sys.modules` across three "tests"): confirmed exactly as the plan states — only the first test to trigger the module's exec gets `<module>` in its trace (`test_one: ['<module>', 'helper']`); every later test that calls the same function gets `['helper']` only, with zero record of `<module>` ever having run. There's no way to discover this dependency by tracing alone; it must be a blanket per-file rule.

Part 2 (residual-fraction estimate over `/home/hubert/voci/voci`, 60 files, function bodies vs. everything else via a splitlines-offset walker):
```
TOTAL: 742,758 chars, 462,013 in function bodies (62.2%), 280,745 residual (37.8%)
```
Worst individual files: `_run/run.py` 72.0% body / 28.0% residual, `_builtins/fixtures.py` 36.4% body / 63.6% residual. Roughly **38% of voci's own source, codebase-wide**, sits in the whole-file-invalidation bucket even under Option B's function-level granularity — and this is a lower bound, since residual counts as one indivisible unit: any edit anywhere in that 38% reruns every test that touched *any* function in the file, not a proportional 38% of tests.

## Q6 — Assertion rewriting survival

Probe: parsed a small test module, ran it through voci's actual `rewrite_asserts` (`voci/_assertions/_vendor/rewrite.py`, matching `_rewrite_test`'s exact `compile(..., dont_inherit=True)` call), traced both the rewritten and un-rewritten versions.

Result: **identical** `(qualname, firstlineno)` sets before and after rewriting — `<module>`, `helper`, `test_simple`, `test_compound`, `TestClass`, `TestClass.test_method`, `TestClass.test_with_message`, all at the same line numbers, same filename. No extra PY_START-firing code objects: the rewriter injects `@py_builtins`/`@pytest_ar` as plain module-level `Import` statements (confirmed via `ast.dump`), which execute inside the existing `<module>` frame rather than creating new callables. Assertion fingerprints keyed to source text (as the plan specifies) are therefore safe from rewriting entirely — nothing about `co_qualname`/`co_firstlineno`/`co_filename` needs special-casing for the rewrite path.

## Q7 — Generated code, zipimport, pyc-only, C callables

- `exec(compile(src, "<string>", "exec"))` → `co_filename == '<string>'` literally, shared indistinguishably by every dynamic-exec caller in the process (same collision as dataclasses' generated `__init__`). Passing a custom label (`"<generated:my_plugin>"`) to `compile()` preserves it verbatim as `co_filename` — a viable convention for code generators to opt into traceability, but nothing enforces it.
- **zipimport**: `co_filename == 'research/testmon/fingerprint-probes/zippkg/lib.zip/zmod.py'` — a real-looking path with the zip container embedded; `Path(...).exists()` on it is `False` (confirmed) since it isn't a real filesystem path. A classifier keyed on `Path.exists()` needs a zipimport-specific check, not just existence.
- **pyc-only module** (source `.py` deleted after compiling): `co_filename == '_src_pmod.py'` — the bare name embedded at compile time, resolves to nothing relative to the current process's cwd (confirmed `exists() == False`). No way to know from the code object alone whether this is first- or third-party.
- **C-implemented callables** (`sorted`, `len`, `str.upper`): zero PY_START records for the calls themselves — confirmed with a 4-call block producing exactly the wrapping frame plus 3 calls to a Python `key=` callback passed into `sorted()`, which *does* fire normally. A dependency mediated entirely inside a C extension (calling back into some other first-party module not visible on the Python stack) is invisible to this tracer, independent of anything else in the design.

## Q8 — DISABLE, dedup, and id-vs-string cost

Microbenchmark: 4,000,000 calls to a trivial function, `median` of 7 runs, comparing against a no-monitoring baseline (also noisy — WSL2 — hence median+min both reported):
```
baseline (no monitoring):                          median 170.1 ms
mode1 format (filename,qualname) + set.add EVERY:  median 373.5 ms  (2.20x)
mode2 DISABLE after first hit:                      median 133.5 ms  (0.79x, i.e. ≈ baseline)
mode3 dedup by id(code), early-return, NO DISABLE:  median 133.3 ms  (0.78x, i.e. ≈ baseline)
mode4 record id(code) only, no dedup, EVERY call:   median 133.6 ms  (0.79x, i.e. ≈ baseline)
```
Surprise: I expected the fixed per-event dispatch cost (trampoline into the callback) to dominate, making a no-DISABLE dedup approach land close to mode1. It doesn't — mode3 (id-keyed dedup, no `DISABLE`) is statistically indistinguishable from `DISABLE` itself and from baseline, reproducing the plan's ~2.5x/~1x gap **without needing `DISABLE` or `restart_events()` at all**. Mode4 isolates why: recording `id(code)` alone (an int) into a set on *every* call, with no dedup short-circuit, is *also* at baseline — so the expense in mode1 is specifically building and hashing a `(str, str)` tuple every call, not "the callback ran" or "something got put in a set." **This is the more important finding for Q8**: a per-(code, test) dedup set keyed by `id(code)` is a materially cheaper alternative to the plan's naive-formatting cost model, cheap enough to be indistinguishable from `DISABLE`'s near-free regime, while being trivially sound at any concurrency (no global disable state, no `restart_events()` race).

Soundness of "keep `DISABLE`, `restart_events()` only when the running-test-set changes": forced the exact race deterministically with `threading.Barrier` — test A calls `shared_target()`, gets recorded and disables the location; test B calls it in the *same window*, strictly before either test starts or ends (the running set is `{A, B}` throughout that window). Result: `{'A': {...}, 'B': set()}` — B's call is lost. Then confirmed `restart_events()` does recover subsequent calls. This shows the proposed mitigation is unsound in the general case: **the race lives entirely inside the interval between two tests' start and end, not at the start/end boundaries themselves**, so a policy that only restarts on set-change events has no trigger point inside the exact window where the race occurs. This isn't an edge case — it's the normal state of affairs at concurrency > 1, where most of a test's runtime is spent with the running set unchanged while other tests are mid-flight.

## Concrete fingerprint and tracer design recommendation

- **Fingerprint key**: `(co_filename, qualname)` for uniquely-named functions; fall back to `(qualname, ordinal position among same-named defs in source order)` for genuine duplicates (redefinition, `if`/`else`, `@overload` stubs) — not raw `firstlineno`, which churns on any unrelated edit above the function.
- **Fingerprint value**: hash of `ast.dump(node, include_attributes=False)` over the function's body, not `ast.get_source_segment` text — cheaper at scale (Q4) and comment/whitespace-insensitive by construction, which is very likely the desired behavior and should be stated as a deliberate choice, not left implicit.
- **Two-tier fingerprint per file**: function-body hash (per qualname) + one "import-time residual" hash covering everything that executes during the module's own import — not just literal top-level statements, but decorator applications, dataclass/attrs class processing, and metaclass hooks (Q3's decorator finding generalizes Risk #2). Any test that recorded any qualname from file F depends on F's import-time residual, unconditionally — there is no way to discover this dependency by tracing, only by the blanket rule (Q5). Budget for this: ~38% of voci's own source is residual by even a generous accounting (Q5 part 2), so this bucket will fire often, not rarely.
- **Generated code**: treat dataclass-style `<string>`/`__create_fn__` and bare `exec(..., "<string>", ...)` code objects as **unattributable — force the enclosing class'/caller's whole-file residual dependency**, since there's no back-reference to resolve. Treat attrs-style `<attrs generated methods module.Class>` filenames as **parseable** — extract `module.Class` and attribute to that class's own fingerprint. Treat zipimport and pyc-only `co_filename`s as **not first-party-resolvable via `Path.exists()`** — needs a zipimport-aware and mtime/hash-in-pyc-aware check, or a conservative "unknown → force full run" fallback.
- **Tracer callback**: record `id(code)` into a per-test `set` and early-return on repeat hits — no `DISABLE`, no `restart_events()`. Measured indistinguishable from `DISABLE`'s cost and immune to its concurrency race (Q8). Resolve `id(code) → (filename, qualname)` once per distinct code object seen (a global `dict[int, CodeType]` populated on first sight, read after the test completes), avoiding string construction in the hot path entirely.

## Surprises

- List/set/dict comprehensions get **no code object at all** in 3.12+ — a plan written against pre-3.12 CPython intuition would over-count these.
- PEP 649's `__annotate__` code objects fire PY_START **only when something actually inspects annotations** (e.g., `typing.get_type_hints`), only on 3.14, with a qualname (`'__annotate__'`) that gives no hint which function it belongs to except co-locating by `firstlineno`.
- `ast.get_source_segment` is **the slow choice**, not the safe/simple one — 3-5x slower than `ast.dump`-hashing and ~12x slower than a correctly cached text-slice, because it re-splits the file on every call.
- The per-event dispatch overhead of `sys.monitoring` is nearly free; **the cost the plan measured at 2.56x is specifically the cost of building and hashing string tuples in the callback**, not the instrumentation mechanism itself — meaning a cheap dedup scheme gets you out of the `DISABLE`/`restart_events()` soundness trap for free, performance-wise.
- The "module-level code" risk in the plan is narrower than reality: it's not just `<module>`'s literal top-level statements that get one-shot-attributed to a single test — decorator application and dataclass/attrs/metaclass processing have the exact same failure mode, and no source-text-based fingerprint scheme catches a change to a decorator's own body without this being treated as part of the same whole-file bucket.
- attrs' synthetic filename (`<attrs generated methods module.Class>`) is quietly more forensics-friendly than dataclasses' generic `<string>` — worth knowing if the tracer wants to special-case common code generators rather than blanket-excluding all of them.
