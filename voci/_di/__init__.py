"""Dependency injection: fixture objects, resolution plans, and the runtime that executes them.

`fixtures.py` defines `Fixture`, the `Depends` sentinel, and the statically-validated
`ResolutionPlan` built by scanning a test's defaults at collection time. `runtime.py` holds
`ScopeStore`, which executes a plan against a single-flight, cache-keyed store of constructed
fixture instances.
"""
