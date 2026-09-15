# `_builtins/fixtures.py` — built-in fixtures

See [rationale.md](../rationale.md) for the index.

**`set_level`'s concurrency hazard is asymmetric.** Logger levels are process-global. Raising a
level can make a sibling capture more than it expected — benign for "this record is present",
harmful only for "nothing was logged". *Lowering* one, to silence a noisy dependency, can make a
sibling's own `set_level(DEBUG)` block capture nothing at all for a record it definitely emitted.
Whether this API is "basically safe" depends on which direction the level moves.

**The `_builtins/fixtures.py`/`_builtins/capture.py` import cycle is deliberate.** The built-in
fixture functions here are stubs; their real providers live in `capture.py`, which imports the
four result types back from this module. The cycle resolves only because the rewiring import sits
at the *bottom* of `fixtures.py`, after every name `capture.py` needs already exists. Move it up
with the other imports and you get a partially-initialized-module `ImportError`.
