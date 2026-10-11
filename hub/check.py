"""Single job-field check used by the hub and the Control ready queue.

Never writes. server.check() adds the live seen-id set; ready_queue passes
a read-only copy. Do not call queue() from here.
"""

import re

from hub.sigverify import resolve_pane

FIELDS = ("id", "from", "to", "lane", "priority", "job", "done-when")
BAD = re.compile(r"send|post|pay|delete|git push|publish", re.I)


def check_job(fields, owner, seen=None):
    """Return a reason string, or '' if the hub may approve the job."""
    if not isinstance(fields, dict):
        return "MALFORMED"
    missing = [name for name in FIELDS if not fields.get(name)]
    if missing:
        return f"MALFORMED missing {', '.join(missing)}"
    if fields["priority"] not in ("urgent", "normal"):
        return "MALFORMED priority"
    if fields["from"] != owner:
        return "SPOOFED"
    if seen is not None and fields["id"] in seen:
        return "DUPLICATE"
    if BAD.search(str(fields["job"])):
        return "REFUSED no send, post, pay, delete, git, or publish"
    mapped = resolve_pane(fields)
    if mapped not in ("1", "2"):
        raw = fields.get("pane", "")
        if raw is None:
            raw = ""
        raw = str(raw).strip()
        if raw and raw not in ("1", "2"):
            return "BAD_PANE"
        return "UNMAPPED"
    return ""
