"""Job board, needs-you, and the lane status file. Status from the verdict line only."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from hub.check import check_job
from hub import lanes
from hub.screen import plain_text, relative_to_handoff, screen_safe
from hub.sigverify import verify_job
from hub.tailread import TAIL_BYTES, read_tail

STATUSES = (
    "queued",
    "picked up",
    "returned",
    "DENIED",
    "REFUSED",
    "SPOOFED",
    "DUPLICATE",
    "STOPPED",
    "ROLLED-BACK",
)
BOARD_CAP = 50

# Verdict line only. Never a job's own `status:` field.
_VERDICT = re.compile(
    r"^(?:VERDICT|verdict):\s*"
    r"(queued|picked up|returned|DENIED|REFUSED|SPOOFED|DUPLICATE|STOPPED|ROLLED-BACK)"
    r"(?:\s+\S+)?\s*$"
)
_FIELD = re.compile(r"^([a-z][a-z0-9-]*): ?(.*)$")
_STATUS_LINE = re.compile(r"^- (\S+) (.*)$")
_NEEDS = re.compile(r"^NEEDS:\s*YES\s*$")

_LANES = lanes.load()
OWNERS = dict(_LANES.owners)
RETURNS = dict(_LANES.returns)
STATUS_FILE = _LANES.status
ET = ZoneInfo("America/New_York")
_MIN_TIME = datetime.min.replace(tzinfo=timezone.utc)


def routed_iso(value):
    """Strip a leading router name from `routed: <router> <ISO>`."""
    text = str(value or "").strip()
    if not text:
        return ""
    tokens = text.split()
    for token in reversed(tokens):
        try:
            datetime.fromisoformat(token)
        except ValueError:
            continue
        return token
    return ""


def friendly_et(value):
    """ISO-with-offset (L-PROTO P4a) shown as friendly ET. Unreadable → empty."""
    text = routed_iso(value)
    if not text:
        return ""
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return ""
    if when.tzinfo is None:
        return ""
    when = when.astimezone(ET)
    hour = when.strftime("%I").lstrip("0") or "0"
    return f"{hour}:{when.strftime('%M %p')} ET"


def _blocks(text):
    lines = re.split(r"\r?\n", text)
    found = []
    index = 0
    while index < len(lines):
        if lines[index].startswith("id: "):
            start = index
            index += 1
            while index < len(lines) and not (
                lines[index].startswith("id: ") or lines[index].startswith("## ")
            ):
                index += 1
            found.append(lines[start:index])
        else:
            index += 1
    return found


def _fields(block_lines):
    fields = {}
    current = None
    for line in block_lines:
        stripped = line.strip()
        if _VERDICT.match(stripped) or _NEEDS.match(stripped):
            current = None
            continue
        match = _FIELD.match(line)
        if match:
            current = match.group(1)
            fields[current] = match.group(2)
        elif current is not None:
            fields[current] = (fields[current] + " " + line).strip()
    return fields


def _block_id(block_lines, fields=None):
    for line in block_lines:
        if line.startswith("id: "):
            return line[4:].strip()
    if fields:
        return (fields.get("id") or "").strip()
    return ""


def verdict_status(block_lines):
    """Last VERDICT line in the block. Ignores `status:`. None if absent."""
    found = None
    for line in block_lines:
        match = _VERDICT.match(line.strip())
        if match:
            found = match.group(1)
    return found


def _read(path):
    return read_tail(path, max_bytes=TAIL_BYTES)


def _return_blocks(handoff):
    by_id = {}
    for owner, name in RETURNS.items():
        text = _read(Path(handoff) / name)
        for block in _blocks(text):
            fields = _fields(block)
            job_id = _block_id(block, fields)
            if not job_id:
                continue
            by_id[job_id] = {
                "owner": owner,
                "file": name,
                "lines": block,
                "fields": fields,
                "text": "\n".join(block),
            }
    return by_id


def _approval_status(row):
    if not isinstance(row, dict):
        return None
    decision = str(row.get("decision") or "")
    reason = str(row.get("reason") or "")
    if decision in STATUSES:
        return decision
    for token in STATUSES:
        if reason == token or reason.startswith(token + " "):
            return token
    if decision == "deny":
        return "DENIED"
    return None


def _approval_rows(path):
    """All approvals.jsonl rows in order. Never raises."""
    found = []
    try:
        text = _read(path)
    except Exception:
        return found
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError, TypeError):
            continue
        if not isinstance(row, dict):
            continue
        job_id = str(row.get("id") or "").strip()
        if job_id:
            found.append(row)
    return found


def approvals_history(path):
    """id -> list of approvals.jsonl rows, oldest first. Never raises."""
    found = {}
    for row in _approval_rows(path):
        job_id = str(row.get("id") or "").strip()
        found.setdefault(job_id, []).append(row)
    return found


def approvals_state(path):
    """id -> last approvals.jsonl row fields. Never raises."""
    found = {}
    for job_id, rows in approvals_history(path).items():
        if rows:
            found[job_id] = rows[-1]
    return found


def approvals_verdicts(path):
    """id -> verdict status from approvals.jsonl. Last matching row wins."""
    found = {}
    for job_id, row in approvals_state(path).items():
        status = _approval_status(row)
        if status:
            found[job_id] = status
    return found


def held_from_approvals(path):
    """Hub held-queue rows from approvals.jsonl. Read-only. Last row wins."""
    rows = []
    for job_id, row in approvals_state(path).items():
        if str(row.get("decision") or "") != "held":
            continue
        rows.append({
            "id": job_id,
            "reason": str(row.get("reason") or "held"),
        })
    return rows


def mapped_status(return_lines=None, approval_row=None, approval_history=None):
    """Return/approvals verdicts win. A return with no approve is returned.

    RAN (approve and/or a return file) is sticky: a later held or
    DUPLICATE re-click never overrides it.
    """
    history = []
    if approval_history:
        history = [row for row in approval_history if isinstance(row, dict)]
    elif isinstance(approval_row, dict):
        history = [approval_row]
    approved = any(str(row.get("decision") or "") == "approve" for row in history)
    return_exists = bool(return_lines)
    ran = approved or return_exists
    ret_verdict = verdict_status(return_lines) if return_lines else None
    approval_verdict = None
    for row in history:
        decision = str(row.get("decision") or "")
        status = _approval_status(row)
        if ran and (decision == "held" or status == "DUPLICATE"):
            continue
        if status:
            approval_verdict = status
    if ret_verdict:
        return ret_verdict
    if approval_verdict:
        return approval_verdict
    if approved and return_exists:
        return "returned"
    if approved:
        return "picked up"
    if return_exists:
        return "returned"
    return "queued"


def _routed_key(value):
    text = routed_iso(value)
    if not text:
        return _MIN_TIME
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return _MIN_TIME
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def job_board(handoff, limit=BOARD_CAP, approvals=None):
    """One row per job id. Job-file verdict lines are ignored."""
    handoff = Path(handoff)
    if approvals is None:
        approvals = handoff.parent / "approvals.jsonl"
    returns = _return_blocks(handoff)
    state = approvals_state(approvals)
    history = approvals_history(approvals)
    rows = []
    seen = set()
    for name, owner in OWNERS.items():
        text = _read(handoff / name)
        for block in _blocks(text):
            fields = _fields(block)
            job_id = _block_id(block, fields)
            if not job_id or job_id in seen:
                continue
            seen.add(job_id)
            back = returns.get(job_id)
            status = mapped_status(
                return_lines=back["lines"] if back is not None else None,
                approval_row=state.get(job_id),
                approval_history=history.get(job_id),
            )
            routed = (fields.get("routed") or "").strip()
            return_text = screen_safe(back["text"], handoff) if back else ""
            rows.append({
                "id": job_id,
                "sender": owner,
                "routed": routed,
                "routed_et": friendly_et(routed),
                "status": status,
                "channel": owner,
                "return_file": RETURNS[owner],
                "return_text": return_text,
            })
    rows.sort(key=lambda row: _routed_key(row.get("routed")))
    if limit <= 0:
        return []
    return rows[-limit:]


def job_return_display(handoff, job_id):
    """Full return, read-only plain text, screen-safe."""
    returns = _return_blocks(Path(handoff))
    back = returns.get(job_id)
    if back is None:
        return ""
    return screen_safe(back["text"], handoff)


def _read_seen(root):
    path = Path(root) / ".seen-ids.txt"
    try:
        if not path.is_file():
            return set()
        return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
    except OSError:
        return set()


def ready_queue(handoff, approvals=None, root=None, seen=None):
    """Jobs that pass side-effect-free check() and verify_job.

    Not the 50-row board. Held refusal rows are excluded. Live-folder
    jobs the hub cannot approve are skipped.
    """
    handoff = Path(handoff).resolve()
    if root is None:
        root = handoff.parent
    root = Path(root).resolve()
    if handoff != (root / "bus").resolve():
        return []
    if approvals is None:
        approvals = root / "approvals.jsonl"
    held_ids = {row["id"] for row in held_from_approvals(approvals)}
    seen_set = set(seen) if seen is not None else _read_seen(root)
    history = approvals_history(approvals)
    state = approvals_state(approvals)
    returns = _return_blocks(handoff)
    rows = []
    found = set()
    for name, owner in OWNERS.items():
        text = _read(handoff / name)
        for block in _blocks(text):
            fields = _fields(block)
            job_id = _block_id(block, fields)
            if job_id:
                fields.setdefault("id", job_id)
            if not job_id or job_id in found:
                continue
            found.add(job_id)
            if job_id in held_ids:
                continue
            reason = check_job(fields, owner, seen=seen_set)
            if reason:
                continue
            sig_reason = verify_job(fields, owner, seen=seen_set)
            if sig_reason:
                continue
            back = returns.get(job_id)
            status = mapped_status(
                return_lines=back["lines"] if back is not None else None,
                approval_row=state.get(job_id),
                approval_history=history.get(job_id),
            )
            if status != "queued":
                continue
            rows.append({"id": job_id, "reason": "ready"})
    return rows


def needs_you(ready=None, held=None, handoff=None, approvals=None, awaiting_approve=None):
    """Ready queue waiting for approval. No side effects. Held rows excluded."""
    if handoff is not None:
        return ready_queue(handoff, approvals=approvals)
    held_ids = set()
    for item in held or []:
        if isinstance(item, dict):
            hid = str(item.get("id") or "").strip()
        else:
            hid = str(item or "").strip()
        if hid:
            held_ids.add(hid)
    rows = []
    seen = set()
    items = list(ready or [])
    if awaiting_approve and not items:
        items = [{"id": item, "reason": "ready"} for item in awaiting_approve]
    for item in items:
        if isinstance(item, dict):
            job_id = str(item.get("id") or "").strip()
            reason = str(item.get("reason") or "ready")
        else:
            job_id = str(item or "").strip()
            reason = "ready"
        if not job_id or job_id in seen or job_id in held_ids:
            continue
        seen.add(job_id)
        rows.append({"id": job_id, "reason": reason})
    return rows


def running_job(handoff, approvals=None):
    """A job whose verdict is `picked up`, or None. Never inferred."""
    for row in job_board(handoff, limit=BOARD_CAP, approvals=approvals):
        if row.get("status") == "picked up":
            return row
    return None


def status_feed(handoff, limit=30):
    """Last `limit` status-file entries. Format: `- <ISO time> <one line>`."""
    path = Path(handoff) / STATUS_FILE
    text = _read(path)
    if not text:
        return []
    found = []
    for line in text.splitlines():
        match = _STATUS_LINE.match(line)
        if not match:
            continue
        found.append({
            "time": match.group(1),
            "time_et": friendly_et(match.group(1)),
            "text": screen_safe(match.group(2), handoff),
            "file": relative_to_handoff(path, handoff),
        })
    return found[-limit:]
