"""The three renderings of an audit.

`payload` and `write_payload` produce `findings.json`, the machine-readable half a later stage
reads; `markdown` and `write_markdown` produce `migration-report.md`, the document a team reads;
`terminal` produces the summary the command prints. All three are pure functions of one `Audit`
and say the same thing about it, blind spots and unread sources included.

`plan` is the fourth, and the only one about a `Conversion` rather than an `Audit`: the decisions
behind a diff, printed beside it.
"""

from __future__ import annotations

from voci_migrate.report.conversion import plan
from voci_migrate.report.markdown import markdown
from voci_migrate.report.markdown import write as write_markdown
from voci_migrate.report.payload import FINDINGS_VERSION, payload
from voci_migrate.report.payload import write as write_payload
from voci_migrate.report.terminal import terminal

__all__ = [
    "FINDINGS_VERSION",
    "markdown",
    "payload",
    "plan",
    "terminal",
    "write_markdown",
    "write_payload",
]
