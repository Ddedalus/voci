set positional-arguments
set working-directory := '..'

# Run the velox-migrate test suite (e.g. `just migrate test -k extractor`)
test *args:
    uv run pytest velox-migrate/tests -n auto "$@"

# Regenerate the checked-in corpus ground-truth dumps, one per supported pytest
corpus-dumps *args:
    uv run python velox-migrate/scripts/refresh_corpus_dumps.py "$@"

# Verify the checked-in corpus dumps still match what the extractor produces
corpus-check:
    uv run python velox-migrate/scripts/refresh_corpus_dumps.py --check
