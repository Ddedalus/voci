# Reading the chain out of `ground-truth.json`

`findings.json` names the override; the dump says what it is made of. Both live in
`.velox-migrate/` by default.

## Dump paths

| Path | Shape |
|---|---|
| `rootpath` | The rootdir every `file` and `path` in the dump is relative to. |
| `fixture_defs` | `{key: def}`. `key` is opaque (`"f10"`) and stable within one dump only. |
| `fixture_defs[k].argname` | The name a test requests. |
| `fixture_defs[k].visibility` | The nodeid the definition rules: a directory, module or class. `"."` is the rootdir under pytest 9.x, `""` under 8.4 and for globally registered plugin fixtures. |
| `fixture_defs[k].autouse` | Decides `VX027`. |
| `fixture_defs[k].argnames` | What the factory's signature asks for, `request` included. |
| `fixture_defs[k].func` | `{module, qualname, file, lineno, wrapped}`. `file` starting `${` or `/` means the fixture is pytest's or a plugin's, not the suite's — never edit those. |
| `items[]` | One per collected test. |
| `items[].nodeid` | Joins to `findings.json`'s `.tests`. |
| `items[].name2fixturedefs[name]` | The resolution chain for `name` **for that test**: furthest→closest, winner last. The override is `[-1]`, the definition it overrides is `[-2]`. |
| `autouse_by_node` | `{node: [argname]}` — where each autouse fixture became visible. |

## The query

`velox-migrate` is installed wherever the audit ran, so use its own resolver rather than
re-deriving the chain — `wiring.overrides()` is the exact function that produced the finding.

```python
import sys
from velox_migrate import model
from velox_migrate.audit import wiring
from velox_migrate.audit.reach import Reach

gt = model.load(sys.argv[1] if len(sys.argv) > 1 else ".velox-migrate/ground-truth.json")
reach = Reach(gt)
for ov in wiring.overrides(gt):
    print(f"{ov.winner.argname} @ {ov.node or '.'}  fan_out={ov.fan_out}  tests={len(ov.tests)}")
    print(f"  overrides {ov.overridden.func.file}:{ov.overridden.func.lineno}")
    for key in sorted(ov.downstream):
        dep = gt.fixture_defs[key]
        outside = [t for t in reach.tests_of_fixture(key) if not wiring.under(ov.node, t)]
        verdict = (
            "MOVABLE (reached only from inside the node)"
            if not outside
            else f"shared: {len(outside)} test(s) outside the node"
        )
        print(
            f"  copy {dep.argname} ({dep.func.file}:{dep.func.lineno}) "
            f"autouse={dep.autouse} -> {verdict}"
        )
```

Against `corpus/fixtures_showcase`:

```
settings @ integration  fan_out=2  tests=2
  overrides conftest.py:21
  copy engine (conftest.py:26) autouse=False -> MOVABLE (reached only from inside the node)
```

`MOVABLE` is strategy 2's precondition, per fixture: nothing outside the node reaches that
definition, so moving its `def` into the overriding `conftest.py` changes no test's resolution and
takes it out of `D`. `shared:` rules strategy 2 out for that member — those tests would lose the
fixture.

## Object reference

- `Override.winner` / `.overridden` — `FixtureDef` for `[-1]` / `[-2]`.
- `Override.downstream` — `frozenset[str]` of `fixture_defs` keys: `D`.
- `Override.fan_out` — `len(downstream) + 1`.
- `Override.node` — `winner.visibility`, the raw nodeid `findings.json` renders as `detail.scope`.
- `Override.autouse` — true when any of winner, overridden or `D` is autouse; this is what raises
  `VX027` instead of `VX005`/`VX006`.
- `wiring.under(node, other)` — is `other` inside `node`. `""`, `"."` and `"/"` contain everything.
- `wiring.in_suite(fixture)` — false for pytest's own and plugin fixtures.
- `Reach.tests_of_fixture(key)` / `.tests_under(node)` — blast radius, the same computation the
  audit charged the finding with.
