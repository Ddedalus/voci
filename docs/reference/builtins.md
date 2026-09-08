# Built-in fixtures

voci ships a handful of fixtures for the things most suites need from a runner. Use them just like any other fixture: `tmp: Annotated[Path, Depends(voci.tmp_path)]`.

## Temporary directories

Every directory these allocate lives under one root per run, a fresh numbered directory in the
platform temp directory that keeps the last three runs' contents for a post-mortem. The
[`--basetemp`](cli.md) flag puts the root somewhere else.

::: voci.tmp_path
    options:
      show_attribute_values: false

::: voci.tmp_path_factory
    options:
      show_attribute_values: false

::: voci.TmpPathFactory
    options:
      merge_init_into_class: false
      show_if_no_docstring: true

## py.path compatibility

`tmpdir` and `tmpdir_factory` are `tmp_path` and `tmp_path_factory` behind the `py.path.local`
surface, for suites whose helpers call `.join`, `.strpath` or `.write` on the directory they are
given. A test asking for both a `tmp_path` and a `tmpdir` gets the same directory under two types.

::: voci.tmpdir
    options:
      show_attribute_values: false

::: voci.tmpdir_factory
    options:
      show_attribute_values: false

::: voci.LegacyPath
    options:
      merge_init_into_class: false
      show_if_no_docstring: true

::: voci.LegacyTmpPathFactory
    options:
      merge_init_into_class: false
      show_if_no_docstring: true

## Captured output

::: voci.capture
    options:
      show_attribute_values: false

::: voci.Capture
    options:
      merge_init_into_class: false
      show_if_no_docstring: true

## Captured log records

::: voci.log_records
    options:
      show_attribute_values: false

::: voci.LogRecords
    options:
      merge_init_into_class: false
      show_if_no_docstring: true

## Test metadata

::: voci.test_info
    options:
      show_attribute_values: false

::: voci.TestInfo
    options:
      merge_init_into_class: false
      show_if_no_docstring: true
