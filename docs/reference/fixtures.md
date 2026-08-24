# Fixtures

A fixture is a function decorated with `@velox.fixture()`.
A test or another fixture can request an instance of such fixure by assigning `Depends(that_function)` in a parameter default. 

The fixture `scope` decides how widely one instance is shared, from a fresh instance per `Depends` up to one for the whole suite.

You can auto-use fixtures via `velox.use`, similar to how a dependency can be declared on a FastAPI router.

::: velox.fixture

::: velox.Depends

::: velox.Scope

::: velox.use
