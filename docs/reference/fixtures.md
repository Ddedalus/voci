# Fixtures

A fixture is a function decorated with `@velox.fixture()`, and a test — or another fixture — asks
for one by putting `Depends(that_function)` in a parameter default. The dependency is the imported
object itself, so there is nothing to look up by name. `Scope` decides how widely one constructed
instance is shared, from a fresh instance per injection site up to one for the whole run, and `use`
attaches a fixture to a module or package for the cases where the point is the setup rather than a
value the test reads. `Fixture` and `Injection` are the objects velox builds out of those
declarations.

::: velox.fixture

::: velox.Depends

::: velox.Scope

::: velox.use

The value `@velox.fixture()` gives back in place of the decorated function. Calling it calls the
underlying function as ordinary Python, with no injection.

::: velox.Fixture
    options:
      merge_init_into_class: false

::: velox.Injection
