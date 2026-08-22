set positional-arguments
set working-directory := '..'

# Assert the assertion-rewrite cold/warm ratio is still within budget (spec/07 §5)
cold-start *args:
    uv run python scripts/bench_cold_start.py "$@"
