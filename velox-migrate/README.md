# velox-migrate

Tooling that migrates a pytest suite to velox. A separate distribution from velox itself: the
runtime never depends on it, and it never becomes part of what a migrated suite ships.

Everything downstream starts from one artifact — a JSON *ground-truth dump* of what pytest
resolved for the suite. Migration is only as trustworthy as its picture of the suite's fixture
wiring, and pytest is the only thing that knows that picture exactly, so the tool asks pytest
instead of deducing it.

## Extracting ground truth

Collect the suite, run nothing, write the dump:

```console
$ velox-migrate extract tests
$ cat .velox-migrate/ground-truth.json
```

Anything after `--` goes to pytest unchanged, so a suite that needs its own flags to collect
still works:

```console
$ velox-migrate extract tests -o ground-truth.json -- -p no:randomly --ignore tests/slow
```

Where the suite collects somewhere velox-migrate cannot be installed — a container, a locked CI
image — `velox_migrate/extractor.py` is a single file that imports only pytest. Copy it in, load
it as a plugin, copy the JSON back out:

```console
$ pytest tests -p extractor --collect-only -q --extractor-out ground-truth.json
```

It supports pytest 8.4 through 9.x and refuses, with a message, anything outside that.

## What the dump holds

Collection is enough to answer the questions a translation has to get right, because pytest has
already answered them by the time the last test is collected:

- **which fixture each test actually gets**, with the whole override chain that decided it,
  ordered furthest-to-closest, so an override and the fixture it overrides are both visible;
- **where each autouse fixture applies**, keyed by the directory, module or class it is visible
  from;
- **pytest's own parametrize ids**, captured as text rather than regenerated, along with the
  parameters and per-case marks behind them;
- **every mark**, with the node it was written on, so a mark inherited from a module or a class
  is distinguishable from one on the test;
- **the resolved ini configuration and the installed plugins**, under whichever spelling of a
  renamed ini key the suite uses.

A dump is ground truth for the environment it was taken in. A suite whose fixtures differ by
platform or plugin version needs one extraction per environment, and each dump records the
environment it came from.

Two things collection cannot see, both of which need the sources rather than the dump:
`request.getfixturevalue(...)` and anything else a fixture decides at runtime, and the contents
of test bodies.

## Reading a dump

```python
from velox_migrate import model

ground_truth = model.load(".velox-migrate/ground-truth.json")
item = ground_truth.item("tests/integration/test_api.py::test_client")

settings = item.resolve("settings")  # the definition this test gets
item.dependencies(settings)  # what it requests, resolved for this test
item.autouse_names  # autouse fixtures reaching it, in setup order
```

`load` refuses a dump rather than reading it under the wrong assumptions: one written by a
different version of the extractor, one from an unsupported pytest, and one from a run that did
not collect the suite cleanly — whether some modules failed to import or pytest never reached
the suite at all. A partial dump would otherwise migrate part of a suite without saying so.

## Development

```console
$ just test-migrate        # the test suite
$ just corpus-dumps        # regenerate the checked-in dumps under corpus/dumps/
$ just corpus-check        # verify those dumps match what the extractor produces
```

`corpus/` holds pytest suites that exist to be extracted from, not run — feature-dense by design,
since they are the input the tooling is tested against. `corpus/dumps/` holds their dumps, one per
supported pytest version, which is what keeps a single model honest across both.
