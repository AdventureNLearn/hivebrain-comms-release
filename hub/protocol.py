"""BUS-PROTOCOL v2 checks (SPEC-L-PROTO-1010 rev 6, P4c). Signature stays first.

New rules: 14 stop, 15 duplicate (P2c), 16 verdict-first, 17 routed, 18 priority.
Rules 11-13 quote ISO-8601 with an explicit offset. Rule 3 is amended.
Rule 9: every reply ends with stop. P4c: signer uses hub field codes.
"""

import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from hub import lanes as _lanes
from hub.sigverify import FIELD_LINE, SIGNED_FIELDS, canonicalize

BANNED_VERBS_PATH = Path(__file__).resolve().parent / "banned-verbs.txt"
SPAN_OK = frozenset(("job", "done-when"))
ZW_CHARS = frozenset("\u200b\u200c\u200d\u2060\ufeff")

# Rule 9: every reply ends with stop. Token is trimmed and case-sensitive.
STOP_TOKEN = "stop"

REQUIRED_FIELDS = SIGNED_FIELDS
NONEMPTY_REQUIRED = (
    "v",
    "id",
    "from",
    "to",
    "lane",
    "priority",
    "time",
    "routed",
    "job",
    "done-when",
)
ALLOWED_PRIORITY = ("urgent", "normal")
ALLOWED_ROUTERS = _lanes.load().routers
VERDICTS = ("RAN", "REFUSED", "STOPPED", "DUPLICATE", "SPOOFED", "DENIED")
VERDICT_LINE = re.compile(
    r"^VERDICT: (RAN|REFUSED|STOPPED|DUPLICATE|SPOOFED|DENIED)(?: [A-Z][A-Z0-9_]*)?$"
)
# P2c: content hash only for these kinds. Id is recorded for every
# non-SPOOFED terminal except when the list cannot be written.
CONTENT_HASH_KINDS = ("RAN", "DENIED", "STOPPED")
RESIGN_ID = re.compile(r"^(?P<base>.+)-r(?P<n>[2-9]|[1-9][0-9]+)$")
SEEN_REPLACE_TRIES = 4
SEEN_REPLACE_BACKOFF = 0.05
ARCHIVE_RETRIES = 4
ARCHIVE_DEADLINE_SEC = 5.0
LOCK_WAIT_SEC = 5.0
LOCK_WAIT_SLICE = 0.05
# Linear ISO-8601 with explicit offset. Cap input length before matching.
# [0-9] so non-ASCII digits (Arabic-Indic, etc.) are refused.
TIME_MAX_LEN = 64
ISO_TIME = re.compile(
    r"^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(\.[0-9]{1,6})?([+-][0-9]{2}:[0-9]{2})$"
)
# Only US Eastern offsets. Z and any other offset are MALFORMED_TIME.
ALLOWED_OFFSETS = ("-04:00", "-05:00")
ET = ZoneInfo("America/New_York")
SEEN_KEYS = ("id", "content", "at", "claim", "pane")
# One-line change. Spec rev 3: keep seen ids at least 30 days.
SEEN_RETENTION_DAYS = 30
JOB_STALE_AFTER = timedelta(hours=24)
JOB_FUTURE_SLACK = timedelta(minutes=5)
SEEN_INIT_SUFFIX = ".init"

_seen_events = []
_approvals_log = None


class SeenListError(Exception):
    """Seen-list unreadable or unparseable. Fail closed; do not rewrite."""


class ArchiveError(Exception):
    """Live-file archive gave up after bounded retries."""


class LockError(Exception):
    """Shared bus lock could not be opened or acquired in time."""


class VerbsUnavailable(Exception):
    """banned-verbs.txt is missing or has no verb lines. Fail closed."""


class Verdict:
    def __init__(
        self,
        kind,
        reason="",
        *,
        ready=False,
        http=None,
        original_id="",
        original_verdict="",
    ):
        self.kind = kind
        self.reason = reason
        self.ready = ready
        self.http = http if http is not None else (reason or kind)
        self.original_id = original_id
        self.original_verdict = original_verdict

    @property
    def queue_reason(self):
        if self.ready:
            return ""
        if self.kind == "SPOOFED":
            return f"SPOOFED {self.reason}" if self.reason else "SPOOFED"
        if self.http:
            return self.http
        if self.reason:
            return self.reason
        return self.kind


def public_fields(fields):
    src = fields if isinstance(fields, dict) else {}
    return {key: value for key, value in src.items() if not str(key).startswith("_")}


def raw_lines(fields):
    src = fields if isinstance(fields, dict) else {}
    stored = src.get("_raw_lines")
    if stored:
        return list(stored)
    lines = []
    for key, value in src.items():
        if str(key).startswith("_"):
            continue
        lines.extend(str(value or "").splitlines())
    return lines


def stop_reason(fields):
    """Rule 14. Exact trimmed 'stop' is STOPPED. Lookalikes are MALFORMED_STOP."""
    for line in raw_lines(fields):
        text = str(line).rstrip("\r\n")
        stripped = text.strip()
        if stripped == STOP_TOKEN:
            return "STOPPED"
        if _malformed_stop(stripped):
            return "MALFORMED_STOP"
    return ""


def _malformed_stop(text):
    if not text or text == STOP_TOKEN:
        return False
    lowered = text.lower()
    if lowered.startswith("stop"):
        return True
    folded = lowered.replace("0", "o")
    return folded == "stop" or folded.startswith("stop")


def priority_reason(fields):
    """Rule 18. Exactly urgent or normal."""
    raw = fields.get("priority", "") if isinstance(fields, dict) else ""
    if raw is None:
        raw = ""
    if str(raw).strip() not in ALLOWED_PRIORITY:
        return "BAD_PRIORITY"
    return ""


def missing_required(fields):
    """Required-field list (P6 fold-in). Priority, routed, and time have own codes."""
    src = public_fields(fields)
    missing = []
    for name in NONEMPTY_REQUIRED:
        if name in ("priority", "routed", "time"):
            continue
        raw = src.get(name, "")
        if raw is None or str(raw).strip() == "":
            missing.append(name)
    return missing


def missing_field_reason(fields):
    if missing_required(fields):
        return "MISSING_FIELD"
    return ""


def normalize_verb_text(text):
    """NFKC, casefold, strip zero-width, collapse whitespace. Hub and signer match."""
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    stripped = "".join(ch for ch in folded if ch not in ZW_CHARS)
    return " ".join(stripped.split())


def load_banned_verbs(path=None):
    target = Path(path) if path is not None else BANNED_VERBS_PATH
    try:
        if not target.is_file():
            raise VerbsUnavailable("VERBS_UNAVAILABLE")
        raw_text = target.read_text(encoding="utf-8")
    except VerbsUnavailable:
        raise
    except OSError as exc:
        raise VerbsUnavailable("VERBS_UNAVAILABLE") from exc
    verbs = []
    for line in raw_text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        verbs.append(raw)
    if not verbs:
        raise VerbsUnavailable("VERBS_UNAVAILABLE")
    return tuple(verbs)


def verb_reason(fields):
    """P4c. Banned phrases in job text. List is hub/banned-verbs.txt only."""
    job = ""
    if isinstance(fields, dict):
        job = fields.get("job", "")
    if job is None:
        job = ""
    try:
        verbs = load_banned_verbs()
    except VerbsUnavailable:
        return "VERBS_UNAVAILABLE"
    text = normalize_verb_text(job)
    for verb in verbs:
        needle = normalize_verb_text(verb)
        if needle and needle in text:
            return "BAD_VERB"
    return ""


def field_span_reason(fields):
    """P4c. Only job and done-when may continue onto another line."""
    lines = None
    if isinstance(fields, dict):
        lines = fields.get("_raw_lines")
    if not lines:
        return ""
    current = None
    for raw in lines:
        line = str(raw).rstrip("\r\n")
        match = FIELD_LINE.match(line)
        if match:
            current = match.group(1)
            continue
        if current and current not in SPAN_OK and line.strip():
            return "BAD_FIELD"
    return ""


def owner_reason(fields, owner):
    """P4c. from must equal the file owner exactly."""
    claimed = ""
    if isinstance(fields, dict):
        claimed = fields.get("from", "")
    if claimed is None:
        claimed = ""
    if str(claimed) != str(owner or ""):
        return "OWNER_MISMATCH"
    return ""


def parse_iso_time(text):
    """ISO-8601 with an explicit Eastern offset. Never raises; None is MALFORMED_TIME."""
    raw = "" if text is None else str(text)
    if len(raw) > TIME_MAX_LEN:
        return None
    raw = raw.strip()
    if raw == "" or len(raw) > TIME_MAX_LEN:
        return None
    match = ISO_TIME.fullmatch(raw)
    if match is None:
        return None
    body, frac, off = match.group(1), match.group(2) or "", match.group(3)
    if off not in ALLOWED_OFFSETS:
        return None
    try:
        return datetime.fromisoformat(body + frac + off)
    except ValueError:
        return None


def friendly_et(dt):
    """Display-only Eastern clock. Never used for signing or checks."""
    if dt is None:
        return ""
    eastern = dt.astimezone(ET)
    hour = eastern.strftime("%I").lstrip("0") or "12"
    return eastern.strftime("%Y-%m-%d ") + hour + eastern.strftime(":%M %p ET")


def job_time_reason(fields, *, now=None):
    """P4a / P2a. MALFORMED_TIME, STALE (>24h), or BAD_TIME (>5 min future)."""
    src = fields if isinstance(fields, dict) else {}
    raw = src.get("time", "")
    if raw is None:
        raw = ""
    raw = str(raw)
    if raw.strip() == "":
        return "MALFORMED_TIME"
    parsed = parse_iso_time(raw)
    if parsed is None:
        return "MALFORMED_TIME"
    clock = now if now is not None else datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    instant = parsed.astimezone(timezone.utc)
    clock = clock.astimezone(timezone.utc)
    if instant > clock + JOB_FUTURE_SLACK:
        return "BAD_TIME"
    if clock - instant > JOB_STALE_AFTER:
        return "STALE"
    return ""


def parse_routed(value):
    raw = "" if value is None else str(value).strip()
    if raw == "":
        return "", None, "NOT_ROUTED"
    router = None
    rest = None
    for name in ALLOWED_ROUTERS:
        prefix = name + " "
        if raw.startswith(prefix):
            router = name
            rest = raw[len(prefix):].strip()
            break
    if router is None or rest is None:
        return "", None, "BAD_ROUTED"
    when = parse_iso_time(rest)
    if when is None:
        return router, None, "BAD_ROUTED"
    return router, when, ""


def routed_reason(fields, *, now=None):
    """Rule 17. routed: <router> <ISO-8601 with offset>."""
    src = fields if isinstance(fields, dict) else {}
    router, when, code = parse_routed(src.get("routed", ""))
    if code:
        return code
    clock = now if now is not None else datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    if when.astimezone(timezone.utc) > clock.astimezone(timezone.utc):
        return "BAD_ROUTED"
    job_when = parse_iso_time(src.get("time", ""))
    if job_when is None:
        return "BAD_ROUTED"
    if when > job_when:
        return "BAD_ROUTED"
    return ""


def content_fingerprint(fields):
    src = public_fields(fields)
    blanked = dict(src)
    blanked["id"] = ""
    blanked["time"] = ""
    blanked["routed"] = ""
    message = canonicalize(blanked).encode("utf-8")
    return hashlib.sha256(message).hexdigest()


def _parse_at(text):
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        else:
            parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _now_stamp(now=None):
    clock = now if now is not None else datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return clock.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _note_seen_event(kind, ids=None):
    _seen_events.append({"kind": kind, "ids": list(ids or [])})


def take_seen_events():
    events = list(_seen_events)
    _seen_events.clear()
    return events


def set_approvals_log(path):
    """Point the history check at the live approvals log (or a test log)."""
    global _approvals_log
    _approvals_log = Path(path) if path is not None else None


def approvals_have_history(log_path=None):
    target = Path(log_path) if log_path is not None else _approvals_log
    if target is None or not Path(target).is_file():
        return False
    try:
        text = Path(target).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return any(line.strip() for line in text.splitlines())


def history_blocks(path):
    """True when approvals have history and the seen file is missing.

    A missing `.init` next to a seen file that exists and parses is an
    upgrade from main: write `.init` once and carry on, unless a job
    approved or run in the last 24 hours is missing from the seen file.
    While this alarm is up, load and record raise SeenListError and must
    not recreate the seen file or the marker.
    """
    if not approvals_have_history():
        return False
    return not Path(path).is_file()


def _naive_as_et(parsed):
    """Naive wall times are America/New_York. fold=1 is the repeated hour."""
    return parsed.replace(tzinfo=ET, fold=1)


def _parse_log_time(text):
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        else:
            parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = _naive_as_et(parsed)
        return parsed
    except ValueError:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            return _naive_as_et(parsed)
        except ValueError:
            return None


def recent_approved_or_run_ids(now=None):
    """Ids approved or run in the last 24 hours. Unreadable times count as recent."""
    target = _approvals_log
    if target is None or not Path(target).is_file():
        return set()
    clock = now if now is not None else datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    window = timedelta(hours=24)
    found = set()
    try:
        text = Path(target).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return set()
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        decision = str(row.get("decision") or "")
        if decision not in ("approve", "RAN"):
            continue
        job_id = str(row.get("id") or "").strip()
        if not job_id:
            continue
        when = _parse_log_time(row.get("time", ""))
        if when is not None and clock - when > window:
            continue
        found.add(job_id)
    return found


def upgrade_seen_gap(path, rows, now=None):
    """True when .init is missing, history exists, and a recent id is absent."""
    target = Path(path)
    if seen_marker_path(target).is_file():
        return False
    if not approvals_have_history():
        return False
    if not target.is_file():
        return False
    present = {str(row.get("id") or "") for row in (rows or []) if row.get("id")}
    missing = recent_approved_or_run_ids(now=now) - present
    return bool(missing)


def _row_from_json(obj):
    if not isinstance(obj, dict):
        raise SeenListError("SEEN_LIST")
    job_id = str(obj.get("id", "") or "").strip()
    if not job_id:
        raise SeenListError("SEEN_LIST")
    return {
        "id": job_id,
        "content": str(obj.get("content", "") or ""),
        "at": str(obj.get("at", "") or ""),
        "claim": str(obj.get("claim", "") or ""),
        "pane": str(obj.get("pane", "") or ""),
    }


def is_claim(row):
    raw = str((row or {}).get("claim", "") or "").strip().lower()
    return raw in ("1", "true", "yes")


def seen_marker_path(path):
    """Sibling marker written on the first successful seen-list read or write.

    First run: neither the seen file nor this marker exists, and the approvals
    log has no history. Absence is normal. The first successful read writes
    this marker (and an empty seen file when the file was missing). Upgrade
    from main: if the seen file exists and parses (plain or JSON) and this
    marker is missing, write the marker once and carry on. After first run,
    if the seen file is missing while the marker remains, or while
    approvals.jsonl has history and the seen file is missing, that is an
    alarm and the watcher fails closed. A plain id that starts with '{' is
    migrated only when the file has no valid JSON rows; otherwise the mix
    alarms. A BOM on the first legacy id is stripped.
    """
    target = Path(path)
    return target.with_name(target.name + SEEN_INIT_SUFFIX)


def _ensure_marker(path):
    if history_blocks(path):
        raise SeenListError("SEEN_LIST")
    marker = seen_marker_path(path)
    if not marker.is_file():
        marker.write_text("1\n", encoding="utf-8")


def resign_parent(job_id):
    match = RESIGN_ID.fullmatch(str(job_id or ""))
    if not match:
        return ""
    return match.group("base")


def store_content_hash(verdict):
    kind = str(getattr(verdict, "kind", "") or "")
    return kind in CONTENT_HASH_KINDS


def _interpret_seen_line(line, now=None):
    text = line.strip()
    if not text:
        return None, "empty"
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return {"id": text, "content": "", "at": _now_stamp(now), "claim": "", "pane": ""}, "plain"
        if isinstance(obj, dict) and str(obj.get("id", "") or "").strip():
            return _row_from_json(obj), "json"
        if isinstance(obj, dict) and any(key in obj for key in ("id", "content", "at")):
            raise SeenListError("SEEN_LIST")
        return {"id": text, "content": "", "at": _now_stamp(now), "claim": "", "pane": ""}, "plain"
    return {"id": text, "content": "", "at": _now_stamp(now), "claim": "", "pane": ""}, "plain"


def expire_seen(rows, now=None):
    clock = now if now is not None else datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    keep = []
    dropped = []
    limit = timedelta(days=SEEN_RETENTION_DAYS)
    for row in rows:
        when = _parse_at(row.get("at", ""))
        if when is None:
            keep.append({key: str(row.get(key, "") or "") for key in SEEN_KEYS})
            continue
        if clock - when <= limit:
            keep.append({key: str(row.get(key, "") or "") for key in SEEN_KEYS})
        else:
            dropped.append(str(row.get("id", "") or ""))
    return keep, dropped


def _fsync_dir(path):
    folder = str(Path(path).parent)
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def bus_lock_path(live_path):
    live = Path(live_path)
    return live.with_name(live.name + ".lock")


def lock_bus(live_path):
    return _FileLock(bus_lock_path(live_path))


def _replace_with_retry(tmp_name, target):
    last = None
    for attempt in range(SEEN_REPLACE_TRIES):
        try:
            os.replace(tmp_name, target)
            return
        except OSError as exc:
            last = exc
            if attempt + 1 < SEEN_REPLACE_TRIES:
                time.sleep(SEEN_REPLACE_BACKOFF * (2 ** attempt))
    raise last


def _dump_seen(path, rows):
    if history_blocks(path):
        raise SeenListError("SEEN_LIST")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({key: row.get(key, "") for key in SEEN_KEYS}, separators=(",", ":")) for row in rows]
    text = "\n".join(lines) + ("\n" if lines else "")
    fd = None
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=target.name + ".",
            suffix=".tmp",
            dir=str(target.parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = None
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(tmp_name, target)
        tmp_name = None
        _fsync_dir(target)
        _ensure_marker(target)
    except OSError as exc:
        raise SeenListError("SEEN_LIST") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _load_seen(path, now=None):
    if history_blocks(path):
        raise SeenListError("SEEN_LIST")
    target = Path(path)
    marker = seen_marker_path(target)
    if not target.is_file():
        if marker.is_file():
            raise SeenListError("SEEN_LIST")
        return [], False
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SeenListError("SEEN_LIST") from exc
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines()
    if lines and lines[0].startswith("\ufeff"):
        lines[0] = lines[0][1:]
    json_rows = []
    plain_rows = []
    for line in lines:
        row, kind = _interpret_seen_line(line, now=now)
        if row is None:
            continue
        if kind == "json":
            json_rows.append(row)
        else:
            plain_rows.append(row)
    if json_rows and plain_rows:
        raise SeenListError("SEEN_LIST")
    if plain_rows:
        return plain_rows, True
    return json_rows, False


def read_seen(path, now=None, alarm_claims=True):
    target = Path(path)
    rows, migrated = _load_seen(path, now=now)
    fresh, dropped = expire_seen(rows, now=now)
    claimed = [row.get("id", "") for row in fresh if is_claim(row) and row.get("id")]
    if alarm_claims and claimed:
        _note_seen_event("unfinished_claim", claimed)
    if migrated:
        _note_seen_event("migrate", [row.get("id", "") for row in fresh])
    if dropped:
        _note_seen_event("prune", [item for item in dropped if item])
    if history_blocks(path):
        raise SeenListError("SEEN_LIST")
    if upgrade_seen_gap(path, fresh, now=now):
        raise SeenListError("SEEN_LIST")
    if migrated or dropped or not target.is_file():
        _dump_seen(path, fresh)
    else:
        _ensure_marker(path)
    return fresh


def seen_ids(path, now=None, alarm_claims=True):
    return {row["id"] for row in read_seen(path, now=now, alarm_claims=alarm_claims) if row.get("id")}


def find_seen(path, job_id, now=None, alarm_claims=True):
    job_id = str(job_id or "")
    if not job_id:
        return None
    for row in read_seen(path, now=now, alarm_claims=alarm_claims):
        if row.get("id") == job_id:
            return row
    return None


def find_content(path, digest, now=None):
    digest = str(digest or "")
    if not digest:
        return None
    for row in read_seen(path, now=now, alarm_claims=False):
        if row.get("content") == digest:
            return row
    return None


def _stamp(at=None):
    stamp = at
    if stamp is None:
        stamp = datetime.now(timezone.utc)
    if isinstance(stamp, datetime):
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        stamp = stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(stamp)


def claim_seen(path, fields, *, at=None, now=None):
    """Atomically write an unfinished claim (id, no hash) before run_pane."""
    if history_blocks(path):
        raise SeenListError("SEEN_LIST")
    src = public_fields(fields)
    job_id = str(src.get("id", "") or "")
    if not job_id:
        raise SeenListError("SEEN_LIST")
    rows = list(read_seen(path, now=now, alarm_claims=False))
    for item in rows:
        if item.get("id") == job_id:
            raise SeenListError("SEEN_LIST")
    rows.append({
        "id": job_id,
        "content": "",
        "at": _stamp(at),
        "claim": "1",
        "pane": str(src.get("pane", "") or ""),
    })
    _dump_seen(path, rows)


def finalize_seen(path, fields, verdict=None, *, at=None, now=None, store_content=None):
    """Replace a claim with the terminal verdict and optional content hash."""
    if history_blocks(path):
        raise SeenListError("SEEN_LIST")
    src = public_fields(fields)
    job_id = str(src.get("id", "") or "")
    if not job_id:
        raise SeenListError("SEEN_LIST")
    if store_content is None:
        store_content = store_content_hash(verdict)
    digest = content_fingerprint(src) if store_content else ""
    stamp = _stamp(at)
    rows = list(read_seen(path, now=now, alarm_claims=False))
    found = False
    for item in rows:
        if item.get("id") == job_id:
            if store_content:
                item["content"] = digest
            item["claim"] = ""
            item["at"] = stamp
            if src.get("pane") and not item.get("pane"):
                item["pane"] = str(src.get("pane") or "")
            found = True
            break
    if not found:
        rows.append({
            "id": job_id,
            "content": digest,
            "at": stamp,
            "claim": "",
            "pane": str(src.get("pane", "") or ""),
        })
    _dump_seen(path, rows)


def record_seen(path, fields, verdict=None, *, at=None, now=None, store_content=None):
    if history_blocks(path):
        raise SeenListError("SEEN_LIST")
    src = public_fields(fields)
    job_id = str(src.get("id", "") or "")
    if not job_id:
        return
    stamp = _stamp(at)
    if store_content is None:
        store_content = store_content_hash(verdict)
    digest = content_fingerprint(src) if store_content else ""
    rows = list(read_seen(path, now=now, alarm_claims=False))
    for item in rows:
        if item.get("id") == job_id:
            if store_content and not item.get("content"):
                item["content"] = digest
                item["at"] = stamp
                item["claim"] = ""
                _dump_seen(path, rows)
            return
    rows.append({
        "id": job_id,
        "content": digest,
        "at": stamp,
        "claim": "",
        "pane": str(src.get("pane", "") or ""),
    })
    _dump_seen(path, rows)


def drop_claim(path, job_id, *, now=None):
    """Remove an unfinished claim so a SPOOFED / PANE_MISMATCH id stays off the list."""
    job_id = str(job_id or "")
    if not job_id:
        return
    if history_blocks(path):
        raise SeenListError("SEEN_LIST")
    rows = list(read_seen(path, now=now, alarm_claims=False))
    kept = [row for row in rows if not (row.get("id") == job_id and is_claim(row))]
    if len(kept) == len(rows):
        return
    _dump_seen(path, kept)


def first_verdict_for_id(text, job_id):
    job_id = str(job_id or "")
    if not job_id:
        return ""
    kind = None
    for line in str(text or "").splitlines():
        if line.startswith("VERDICT: "):
            parts = line.split()
            kind = parts[1] if len(parts) > 1 else ""
            continue
        if kind and line.startswith("id: ") and line[4:].strip() == job_id:
            return kind
        if line == STOP_TOKEN:
            kind = None
    return ""


def format_reply(verdict, fields, extra=None):
    kind = str(getattr(verdict, "kind", "") or "")
    reason = str(getattr(verdict, "reason", "") or "")
    if kind not in VERDICTS:
        kind = "REFUSED"
        if not reason:
            reason = "MALFORMED"
    line1 = f"VERDICT: {kind}"
    if reason:
        line1 = f"{line1} {reason}"
    if VERDICT_LINE.fullmatch(line1) is None:
        line1 = "VERDICT: REFUSED"
        kind = "REFUSED"
    lines = [line1]
    src = public_fields(fields)
    job_id = str(src.get("id", "") or "")
    if job_id:
        lines.append(f"id: {job_id}")
    parent = resign_parent(job_id)
    if parent:
        lines.append(f"prior-id: {parent}")
    parsed = parse_iso_time(src.get("time", ""))
    display = friendly_et(parsed)
    if display:
        lines.append(f"time-et: {display}")
    if kind == "DUPLICATE":
        original_id = str(getattr(verdict, "original_id", "") or "")
        original_verdict = str(getattr(verdict, "original_verdict", "") or "")
        if original_id:
            lines.append(f"original-id: {original_id}")
        if original_verdict:
            lines.append(f"original-verdict: {original_verdict}")
    if extra:
        lines.extend(str(item) for item in extra if item is not None)
    lines.append(STOP_TOKEN)  # rule 9: every reply ends with stop
    text = "\n".join(lines)
    first = text.splitlines()[0] if text else ""
    if VERDICT_LINE.fullmatch(first) is None:
        return "VERDICT: REFUSED\n" + STOP_TOKEN
    return text


def last_reply(text):
    body = text if isinstance(text, str) else ""
    idx = body.rfind("\nVERDICT: ")
    if idx >= 0:
        return body[idx + 1:]
    if body.startswith("VERDICT: "):
        return body
    idx = body.find("VERDICT: ")
    if idx >= 0:
        return body[idx:]
    return ""


def et_date(when=None):
    clock = when if when is not None else datetime.now(ET)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=ET)
    return clock.astimezone(ET).strftime("%Y-%m-%d")


def archive_name(live_name, when=None):
    name = str(live_name or "")
    if name.endswith(".md"):
        name = name[:-3]
    return name + "-ARCHIVE-" + et_date(when) + ".md"


def return_ids_in(text):
    found = set()
    for line in str(text or "").splitlines():
        if line.startswith("id: "):
            job_id = line[4:].strip()
            if job_id:
                found.add(job_id)
    return found


def extract_finished(text, finished_ids):
    """Split live bus text into (archived_blocks, remaining_text)."""
    wanted = {str(item) for item in (finished_ids or set()) if item}
    if not text:
        return "", ""
    lines = str(text).splitlines(keepends=True)
    taken = []
    kept = []
    index = 0
    while index < len(lines):
        line = lines[index]
        bare = line.splitlines()[0] if line else ""
        if bare.startswith("id: "):
            job_id = bare[4:].strip()
            start = index
            index += 1
            while index < len(lines):
                nxt = lines[index].splitlines()[0] if lines[index] else ""
                if nxt.startswith("id: ") or nxt.startswith("## "):
                    break
                index += 1
            chunk = "".join(lines[start:index])
            if job_id in wanted:
                taken.append(chunk)
            else:
                kept.append(chunk)
        else:
            kept.append(line)
            index += 1
    return "".join(taken), "".join(kept)


class _FileLock:
    def __init__(self, path):
        self.path = Path(path)
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_WAIT_SEC
        while True:
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o644)
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise LockError("LOCK")
                time.sleep(LOCK_WAIT_SLICE)
                continue
            except OSError as exc:
                raise LockError("LOCK") from exc
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None
                if time.monotonic() >= deadline:
                    raise LockError("LOCK")
                time.sleep(LOCK_WAIT_SLICE)

    def __exit__(self, exc_type, exc, tb):
        if self.fd is None:
            return False
        try:
            if os.name == "nt":
                import msvcrt
                try:
                    os.lseek(self.fd, 0, os.SEEK_SET)
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None
        return False


def _atomic_write_text(path, text):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=target.name + ".",
            suffix=".tmp",
            dir=str(target.parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = None
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(tmp_name, target)
        tmp_name = None
        _fsync_dir(target)
    except OSError as exc:
        raise SeenListError("STORE_ERROR") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def append_bus_locked(live_path, text):
    """Sender helper: append to a live bus file under the shared lock."""
    live = Path(live_path)
    extra = str(text or "")
    if not extra:
        return
    with lock_bus(live):
        current = live.read_text(encoding="utf-8") if live.is_file() else ""
        _atomic_write_text(live, current + extra)


def move_finished_to_archive(live_path, finished_ids, *, during=None, changing=None, now=None):
    """Append finished blocks to the dated archive, then rewrite the live file.

    Every writer (hub, signer, senders' helper) takes the same
    ``<file>.lock``. Read and replace run inside that lock. Retries are
    capped; giving up raises ArchiveError so the caller can alarm.
    """
    live = Path(live_path)
    wanted = {str(item) for item in (finished_ids or set()) if item}
    if not wanted:
        return ""
    dest = live.with_name(archive_name(live.name, now))
    last_taken = ""
    deadline = time.monotonic() + ARCHIVE_DEADLINE_SEC
    with lock_bus(live):
        hook = during
        attempts = 0
        while True:
            if time.monotonic() >= deadline:
                raise ArchiveError("ARCHIVE")
            text = live.read_text(encoding="utf-8") if live.is_file() else ""
            already = return_ids_in(dest.read_text(encoding="utf-8") if dest.is_file() else "")
            fresh = wanted - already
            taken, _ignored = extract_finished(text, fresh)
            _all_taken, kept = extract_finished(text, wanted)
            if taken:
                with dest.open("a", encoding="utf-8") as handle:
                    handle.write(taken if taken.endswith("\n") else taken + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                last_taken = taken
            if hook is not None:
                hook()
                hook = None
            if changing is not None:
                changing()
            text2 = live.read_text(encoding="utf-8") if live.is_file() else ""
            if text2 == text:
                _atomic_write_text(live, kept)
                return last_taken
            attempts += 1
            if attempts >= ARCHIVE_RETRIES:
                raise ArchiveError("ARCHIVE")
