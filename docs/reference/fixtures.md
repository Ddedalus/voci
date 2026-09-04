# Fixtures

A fixture is a function decorated with `@velox.fixture()`.
A test or another fixture can request an instance of such fixture by naming `Depends(that_function)` on a parameter — in its default, or in its `Annotated[...]` metadata.

The fixture `scope` decides how widely one instance is shared, from a fresh instance per `Depends` up to one for the whole suite.

You can auto-use fixtures via `velox.use`, similar to how a dependency can be declared on a FastAPI router.

::: velox.fixture

::: velox.Depends

::: velox.Scope

::: velox.use

## Where the injection is declared

A parameter declares its injection in its default or in its `Annotated[...]` metadata:

```python
async def test_balance(db: Annotated[Session, Depends(db_fx)]) -> None: ...


async def test_balance(db: Session = Depends(db_fx)) -> None: ...
```

The two run identically. Declaring one parameter both ways is an error, naming the parameter, even
when both name the same fixture.

In metadata the parameter takes no default, so an injected parameter can precede a parameter that
has none — one supplied by `@velox.parametrize`, say — and calling the test by hand takes an
ordinary `Session` rather than a sentinel. An alias carries a marker as well as a signature does:

```python
type Db = Annotated[Session, Depends(db_fx)]


async def test_balance(db: Db) -> None: ...
```

velox parses the annotation rather than evaluating it, reading its source text with `ast` and
evaluating only the `Depends(...)` calls it finds in metadata. The type half is never evaluated,
so in a module with `from __future__ import annotations` — where Python does not evaluate it
either — a type imported under `if TYPE_CHECKING:` is a fine thing to inject against.

What velox does evaluate, it evaluates in the module's globals, which is where the fixture named
in `Depends(...)` and any alias carrying a marker have to be reachable: a fixture held in a local
variable is not, and velox says so at collection, naming the parameter.

## Typing an injected parameter

`@velox.fixture()` gives a fixture its value type, unwrapping whatever the function yields, awaits
or returns, so `Depends(db_fx)` is a `Session` wherever `db_fx` is a `Fixture[Session]`. The
parameter that receives it takes its type from its own annotation, and type checkers disagree
about what to do when there is none:

| | `db: Annotated[Session, Depends(db_fx)]` | `db: Session = Depends(db_fx)` | `db=Depends(db_fx)` |
| --- | --- | --- | --- |
| mypy | `Session` | `Session` | `Any` |
| pyright | `Session` | `Session` | `Session` |
| pyrefly | `Session` | `Session` | `Session` |

Both spellings run the same way, and the short one is a fine thing to write. Its cost is confined
to mypy, and it is larger than one parameter. An injected parameter with no annotation is `Any`,
so `db.no_such_method()` and `wrong: str = db` both pass; and a test whose parameters are *all*
injected the short way carries no annotation at all, which makes it an untyped function whose body
mypy does not check in the first place. `--strict` reports the missing annotation and still says
nothing about the body. Writing the annotation gets the body checked under every checker, which is
the reason to write it.

The case values a fixture's `params=` takes reach the body through a parameter named `param`, and
nothing checks that the annotation on that parameter agrees with them: a body annotating
`param: str` over `params=[1, 2, 3]` type-checks. `param` is bound by name at construction time,
where the decorator has no say over the signature it is handed.

## Fixtures whose value is an iterator

A fixture declared `-> Iterator[X]` reads as one that yields an `X`, and its injection sites are
typed `X`. A fixture that *returns* an iterator declares the same signature, so its sites are typed
`X` too, while the value they are handed is the iterator — velox dispatches on whether the function
is a generator, not on its annotation. Where the iterator is the value, declare it as
`Iterable[X]`: that is equally true of an iterator, and it is not one of the shapes
`@velox.fixture()` unwraps.
