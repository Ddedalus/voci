# Migrating from pytest

`velox-migrate` takes a pytest suite and turns it into a velox one. It is a separate distribution
from velox: the runtime never depends on it, and nothing it installs ends up in the migrated
suite.

```bash
uv pip install velox-migrate    # or: pip install velox-migrate
```

A migration is four commands, run in order, each reading what the one before it wrote:

| Command | Does |
|---|---|
| `velox-migrate extract` | asks pytest what it resolved for the suite, and writes it down |
| `velox-migrate audit` | classifies every construct and reports what migrating would cost |
| `velox-migrate convert` | rewrites the suite in velox's spelling |
| `velox-migrate verify` | runs both runners and compares them test for test |

Everything downstream starts from that first artifact. A migration is only as good as its picture
of the suite's fixture wiring, and pytest is the only thing that knows that picture exactly — so
the tool asks pytest rather than deducing it from the sources.

## How much of pytest carries over

Most of a suite is a spelling change. Fixtures, parametrization, marks, the capture and temporary
directory builtins, `raises` and `approx` all have a velox counterpart the conversion writes for
you:

| pytest | velox |
| --- | --- |
| `conftest.py` and name-based lookup | a module you import, and `Depends(fixture)` |
| fixture scopes: function, class, module, package, session | call, function, module, session — class widens to module, package to session |
| `autouse=True`, `@pytest.mark.usefixtures` | one `velox.use(...)` on the module or package |
| `@pytest.mark.parametrize`, `pytest.param` | `@velox.parametrize`, `velox.case`, with pytest's ids kept verbatim |
| indirect parametrization | `params=` on the fixture itself |
| `skip`, `skipif`, `xfail`, custom marks | `@velox.skip`, `@velox.skipif`, `@velox.xfail`, `@velox.tag` |
| `capsys`, `caplog`, `tmp_path`, `tmpdir` | `velox.capture`, `velox.log_records`, `tmp_path`, `velox.tmpdir` |
| `pytest.raises`, `pytest.approx` | `velox.raises`, `velox.approx` |
| `pytest-asyncio`, `anyio`, `event_loop` fixtures | deleted — velox runs async tests itself |
| `pytest-xdist` | deleted — velox is concurrent within one process |

Some of it has no counterpart, and a suite leaning on one of these has to give it up or keep that
part under pytest:

| pytest | Why velox has nothing for it |
| --- | --- |
| `unittest.TestCase`, doctests, nose-style collection | velox collects functions and `class Test*` methods, and nothing else |
| `conftest.py` hooks, `pytest_addoption`, plugin-provided fixtures and marks | there is no hook protocol to plug into: injection is the extension point, and no hook dispatch on the hot path is a design invariant |
| `pytest.warns`, `recwarn`, `deprecated_call`, `filterwarnings` | the warnings filter is one process-global list, and velox's tests share the process |
| `capfd`, `capsysbinary`, `capfdbinary` | velox captures by replacing `sys.stdout` and `sys.stderr`, so a write to file descriptor 1 by a subprocess or a C extension goes uncaptured, and there is no binary variant |
| `pytestconfig`, `cache`, `record_property`, `pytester` | each is a handle on pytest's own machinery |
| `pytest.importorskip`, `pytest.xfail()` as a statement | a decision made partway through a body has no runtime call to make it with — guard the import and use `@velox.skipif`, or mark the test `@velox.xfail` outright |
| a non-asyncio event loop — trio, tornado | velox runs the suite on one asyncio loop |

The plugin and hook system, and `unittest`/`doctest`/`nose` collection, are absent by design
rather than pending.

A smaller group converts only once you have edited the suite by hand — xunit `setup_method` and
friends, a `conftest.py` override whose specialized chain would fan out past the budget, a
`request` object passed to another function. The audit counts each as blocked and names the file
and line, so the work is a list rather than a search.

A last category converts untouched and then costs you concurrency. `monkeypatch`, writes to
`os.environ`, `unittest.mock.patch`, `mocker`, a frozen clock, `os.chdir` — anything patching state
the whole process shares — makes velox schedule that test alone, since nothing else can safely run
beside it. The audit reports that share of the suite up front, because it is the part concurrency
cannot speed up.

The [support matrix](matrix.md) is the full table: every construct, its code, what it becomes, and
what to do where the answer is nothing.

## Extract the ground truth

Collect the suite, run nothing, write the dump:

```console
$ velox-migrate extract tests
42 tests collected in 0.03s
velox-migrate: wrote ground truth for 42 tests to .velox-migrate/ground-truth.json
```

Anything after `--` goes to pytest unchanged, so a suite that needs its own flags in order to
collect still works:

```console
$ velox-migrate extract tests -- -p no:randomly --ignore tests/slow
```

The dump records which fixture each test actually gets and the whole override chain that decided
it, where each autouse fixture applies, pytest's own parametrize ids, every mark together with the
node it was written on, and the resolved ini configuration and installed plugins.

A dump is ground truth for the environment it was taken in, and records that environment. A suite
whose fixtures differ by platform or plugin version needs one extraction per environment.

Where the suite collects somewhere `velox-migrate` cannot be installed — a container, a locked CI
image — `velox_migrate/extractor.py` is a single file that imports only pytest. Copy it next to
the suite, load it as a plugin, and copy the JSON back out:

```console
$ pytest tests -p extractor --collect-only -q --extractor-out ground-truth.json
```

## Audit before converting

`audit` answers the question that comes before any rewriting: what is this suite made of, and what
would migrating it cost? It joins the dump with a static read of the suite's own sources,
classifies what it finds, and writes a report. It reads; it changes nothing.

```console
$ velox-migrate audit
42 tests   39 clean (92.9%)   3 need review   0 blocked (0.0%)
serial: 0 of 42 tests run alone, 0.0% of the suite
9 findings over 8 constructs, 4 about the suite itself

VX003  marker      2  fixture scope="class" or scope="package"
VX005  mechanical  1  a fixture overriding one visible from further out
VX103  marker      1  @pytest.mark.skipif with a string condition
VX301  mechanical  1  testpaths
...

types: 6 of 14 fixtures have no return annotation, costing 11 injected parameters — the report lists them worst first

wrote .velox-migrate/migration-report.md and .velox-migrate/findings.json
```

The first line is the whole verdict, and every collected test lands in exactly one of its three
counts. *Clean* converts with nobody reading the diff line by line. *Needs review* converts, but
something shifted enough that the converted source carries a marker, or the test behaves
differently once tests overlap. *Blocked* has no conversion path as it stands, because the
construct needs a human decision.

The serial figure cuts across all three: it is the share of the suite that ends up running alone,
which is the part concurrency cannot speed up. A test can convert untouched and still have to run
by itself.

Every finding cites a code — `VX003`, `VX401` — that names one row of the support matrix: the
pytest construct, what becomes of it, and what to do where the answer is "nothing". The same codes
label report sections and the markers left in converted source, so a number in the summary, a
paragraph in the report and a marker in a file are one thing seen three ways.

`migration-report.md` is written to be read and forwarded: the verdict, then the fixture return
types worth adding first, then a section per construct that needs a decision with its
file-and-line list, then what converts with a caveat, the concurrency hazards, what the conversion
rewires, the configuration and plugins, and last what the audit cannot see. `findings.json`
carries the same content for tooling.

Two things collection cannot see, and the audit names rather than counts: what a fixture decides
at run time, such as `request.getfixturevalue(...)`, and anything behind application code — test
order dependence, teardown timing, shared state.

## Annotate before converting

The conversion preserves whatever type information the suite already states, and fabricates none.
An injected parameter's type comes from the fixture factory's return annotation, so a fixture
written without one is injected into a parameter with nothing to type it: mypy reads such a
parameter as `Any` and checks nothing done with it. The conversion report names every site that
lands that way.

That makes the last step before `convert` a pytest one. Annotate the fixtures, run your type
checker, and keep pytest green while you do it, since a return annotation changes no behaviour:

```python
@pytest.fixture
def db_session() -> Session:
    return Session(engine)
```

The audit's *fixture return types* section is the worklist. Every fixture whose factory has no
return annotation, with its file and line, ordered by how many injections lose their type with it,
so the top of the list is worth the most:

```
- conftest.py:23 (engine) — 5 injections
- conftest.py:28 (client) — 3 injections
- conftest.py:18 (settings) — 2 injections
```

## Convert

`convert` prints its plan and a diff, and writes nothing until you pass `--write`:

```console
$ velox-migrate convert
17 fixtures translated, 0 left as pytest wrote them
12 files change, 3 markers written

fixture modules:
  api/conftest.py -> api/fixtures.py
  conftest.py -> fixtures.py
  api/test_api.py: from api.fixtures import payload as api_payload

translated:
  VX104  6  @pytest.mark.skip, @pytest.mark.skipif(bool)
  VX101  4  @pytest.mark.parametrize
  ...

[tool.velox]:
  testpaths = ["."]
  ignore = ["scratch"]

diff --git a/api/conftest.py b/api/fixtures.py
...

nothing written; pass --write to apply
```

Each `conftest.py` becomes an ordinary module, and the tests that used to pick its fixtures up by
name import them instead. A directory's fixtures go from this:

```python
# api/conftest.py
import pytest


@pytest.fixture
def payload():
    return {"kind": "order", "qty": 2}


@pytest.fixture
def route(payload):
    return f"/v1/{payload['kind']}s"
```

to this:

```python
# api/fixtures.py
import velox
from velox import Depends


@velox.fixture()
def payload():
    return {"kind": "order", "qty": 2}


@velox.fixture()
def route(payload=Depends(payload)):
    return f"/v1/{payload['kind']}s"
```

Where a construct converts but the result deserves a look, the rewrite leaves a marker naming the
code and what to check:

```python
# VELOX-TODO[VX003]: Confirm the fixture tolerates being shared more widely, or split it.
```

Grepping for `VELOX-TODO` is the review queue the conversion leaves behind.

## Verify against pytest

`verify` runs both runners and compares them test for test: pytest on the tree that still holds
the pytest suite, velox on the converted one. Ids survive the conversion verbatim, so the two
verdicts line up by id, and every id they disagree about is the review queue.

```console
$ velox-migrate verify --before ../service-pytest --after .
1188 tests under pytest   1188 under velox   1183 agreed   5 diverged
outcome (5) — ran under both and ended differently
  tests/test_registry.py::test_serializer_must_have_meta            passed -> failed
  ...

wrote .velox-migrate/verify-report.md and .velox-migrate/verify.json
```

The exit status is 0 when everything agreed and 1 when anything did not, so the comparison can be
the gate on a migration branch. `verify-report.md` lists the whole queue — the terminal summary
caps each kind — and `verify.json` carries the same content for tooling.

`convert --write` rewrites the tree in place, which leaves no pytest suite to run afterwards.
Record the pytest half first, then compare against the recording:

```console
$ velox-migrate verify --record --before .   # -> .velox-migrate/pytest-outcomes.json
$ velox-migrate convert --write
$ velox-migrate verify --after .
```

Both halves run in one environment, since one command runs both. Where pytest's half has to run
somewhere `velox-migrate` cannot be installed, `velox_migrate/outcomes.py` is a single file that
imports only pytest — the same arrangement as the extractor. Copy it in, run the suite, and pass
the JSON back as `--baseline`:

```console
$ pytest -p outcomes --outcomes-out pytest-outcomes.json
```

## Then raise the concurrency

velox runs at `--concurrency 1` under `verify` unless `-c` says otherwise. A serial run is what
separates "the conversion changed what the suite does" from "the suite does not survive tests
overlapping". Once the serial comparison agrees, raise it:

```console
$ velox-migrate verify --after . -c 16
```

Anything that diverges now is state shared between tests, and the audit's hazard census is the
index to triage it with.
