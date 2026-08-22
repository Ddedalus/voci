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

## Auditing a suite

`audit` answers the question that comes before any rewriting: what is this suite made of, and what
would migrating it cost? It joins the dump with a static read of the suite's own sources, classifies
every construct it finds, and writes what it concludes. It reads; it changes nothing.

```console
$ velox-migrate audit
891 tests   692 clean (77.7%)   194 need review   5 blocked (0.6%)
serial: 201 of 891 tests run alone, 22.6% of the suite
101 findings over 14 constructs, 5 about the suite itself
...
wrote .velox-migrate/migration-report.md and .velox-migrate/findings.json
```

The four counts are the whole verdict, and every collected test lands in exactly one of them:

- **clean** — converts with nobody reading the diff line by line;
- **needs review** — converts, and something about it shifted enough that the converted source
  carries a `VELOX-TODO[category]` marker, or the test behaves differently once tests overlap;
- **blocked** — no conversion path as it stands, because the construct needs a human decision or
  velox provides nothing like it;
- **serial** — the share of the suite that ends up running alone, which is the part concurrency
  cannot speed up. This one cuts across the other three: a test can convert untouched and still
  have to run by itself.

Every finding cites a code, `VX214` or `VX401`, that names one row of the support matrix: the pytest
construct, what becomes of it, and what to do where the answer is "nothing". The same codes label
report sections and the markers left in converted source, so a number in the summary, a paragraph in
the report and a marker in a file are one thing seen three ways.

`migration-report.md` is written to be read and forwarded: the verdict, then a section per construct
that needs a decision with its file-and-line list, then what converts with a caveat, the
concurrency hazards, what the conversion rewires, the configuration and plugins, and last what the
audit cannot see. `findings.json` carries the same content for tooling, with the matrix rows for
every code it uses.

```console
$ velox-migrate audit --dump ground-truth.json --root ../service --out audit/
$ velox-migrate audit --budget 12     # allow longer generated override chains
```

A conftest fixture that overrides one from a parent directory has no counterpart in velox, where a
dependency names one object: the override and everything between it and the tests that reach it are
copied per overriding directory. `--budget` is how many fixtures one override may cause to be
copied before the audit refuses it instead, and the report names the fan-out either way.

An audit says what it does not know. Constructs no dump and no parse can see — test-order
dependence, teardown timing, state behind application code — are named in the report rather than
counted, and any source file that could not be read is listed with a note that the body-level counts
are lower bounds while it is.

## Development

```console
$ just migrate test         # the test suite
$ just migrate corpus-dumps # regenerate the checked-in dumps under corpus/dumps/
$ just migrate corpus-check # verify those dumps match what the extractor produces
```

`corpus/` holds pytest suites that exist to be extracted from, not run — feature-dense by design,
since they are the input the tooling is tested against. `fixtures_showcase` is the wiring: overrides,
autouse, parametrization, wrapped fixtures. `hazards_showcase` is everything that does not translate
cleanly, one case per matrix row. The rest are the conversion's own bars, each converted and then
run under velox: `mechanical_showcase` for the constructs that translate one for one,
`declarations_showcase` for the fixtures a test gets without naming them, `overrides_showcase` for
the specialized chains, `bodies_showcase` for what only a body shows — patching, and a fixture
asked for by name — and `parametrize_showcase` for the cases a call site decided rather than the
fixture: an `indirect` mark, and a `pytest_generate_tests` hook. `corpus/dumps/` holds their
dumps, one per supported pytest version, which is what keeps a single model honest across both.
