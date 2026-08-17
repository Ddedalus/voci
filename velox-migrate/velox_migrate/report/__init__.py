"""The three renderings of an audit.

`payload` and `write_payload` produce `findings.json`, the machine-readable half a later stage
reads; `markdown` and `write_markdown` produce `migration-report.md`, the document a team reads;
`terminal` produces the summary the command prints. All three are pure functions of one `Audit`
and say the same thing about it, blind spots and unread sources included.
"""

from __future__ import annotations

from velox_migrate.report.markdown import markdown
from velox_migrate.report.markdown import write as write_markdown
from velox_migrate.report.payload import FINDINGS_VERSION, payload
from velox_migrate.report.payload import write as write_payload
from velox_migrate.report.terminal import terminal

__all__ = [
    "FINDINGS_VERSION",
    "markdown",
    "payload",
    "terminal",
    "write_markdown",
    "write_payload",
]
