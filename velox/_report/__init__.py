"""Reporting: renders collected results as they finish and as a final summary.

`terminal.py` holds the jest-style reporter: streaming per-file blocks in real completion order,
then failure details and the short summary in logical (collection) order once the run ends.
`json_report.py` holds `--report-json`, the machine-readable alternative: one JSON object written
once, after the run ends. `collect_json.py` holds `--co-json`, the same idea for `--collect-only`:
one JSON object printed to stdout instead of the plain-text id list.
"""
