# Built-in fixtures

velox ships a handful of fixtures for the things most suites need from a runner: a temporary
directory, the output and log records a test produced, and the identity of the test itself. They
are asked for exactly like your own — import the name and put it in a parameter default, as
`Depends(velox.tmp_path)` — and each one is paired below with the type it hands to the test.

## Temporary directories

Every directory these allocate lives under one root per run, a fresh numbered directory in the
platform temp directory that keeps the last three runs' contents for a post-mortem. The
[`--basetemp`](cli.md) flag puts the root somewhere else.

::: velox.tmp_path
    options:
      show_attribute_values: false

::: velox.tmp_path_factory
    options:
      show_attribute_values: false

::: velox.TmpPathFactory
    options:
      merge_init_into_class: false
      show_if_no_docstring: true

## py.path compatibility

`tmpdir` and `tmpdir_factory` are `tmp_path` and `tmp_path_factory` behind the `py.path.local`
surface, for suites whose helpers call `.join`, `.strpath` or `.write` on the directory they are
given. A test asking for both a `tmp_path` and a `tmpdir` gets the same directory under two types.

::: velox.tmpdir
    options:
      show_attribute_values: false

::: velox.tmpdir_factory
    options:
      show_attribute_values: false

::: velox.LegacyPath
    options:
      merge_init_into_class: false
      show_if_no_docstring: true

::: velox.LegacyTmpPathFactory
    options:
      merge_init_into_class: false
      show_if_no_docstring: true

## Captured output

::: velox.capture
    options:
      show_attribute_values: false

::: velox.Capture
    options:
      merge_init_into_class: false
      show_if_no_docstring: true

## Captured log records

::: velox.log_records
    options:
      show_attribute_values: false

::: velox.LogRecords
    options:
      merge_init_into_class: false
      show_if_no_docstring: true

## Test metadata

::: velox.test_info
    options:
      show_attribute_values: false

::: velox.TestInfo
    options:
      merge_init_into_class: false
      show_if_no_docstring: true
