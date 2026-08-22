set working-directory := '..'

# Re-vendor pytest's assertion subsystem from the oss/pytest submodule (spec/07)
update:
    uv run python scripts/vendor_assertion.py

# Verify the vendored tree matches what the vendoring script generates
check:
    uv run python scripts/vendor_assertion.py --check
