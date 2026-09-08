set positional-arguments
set working-directory := '..'

# Run the voci-migrate test suite (e.g. `just migrate test -k extractor`)
test *args:
    uv run pytest voci-migrate/tests -n auto "$@"

# Regenerate the checked-in corpus ground-truth dumps, one per supported pytest
corpus-dumps *args:
    uv run python voci-migrate/scripts/refresh_corpus_dumps.py "$@"

# Verify the checked-in corpus dumps still match what the extractor produces
corpus-check:
    uv run python voci-migrate/scripts/refresh_corpus_dumps.py --check
