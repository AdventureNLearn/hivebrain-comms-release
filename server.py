"""HiveBrainComms lab. Loopback only. Lab bus only. No live handoff."""

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web

from hub.sigverify import (
    ignore_disable_settings,
    parse_block_lines,
    resolve_pane,
    verify_job,
)
from hub.status import attach_pane_identity, handle_restart_build, handle_status
from hub.win_child_env import WIN_CHILD_ENV
from hub import lanes, protocol

_os_replace = os.replace

try:
    import winpty
except ImportError:
    winpty = None

HOST = "127.0.0.1"
DEFAULT_HUB_PORT = 8107
HUB_PORT_MIN = 1024
HUB_PORT_MAX = 65535
PORT = DEFAULT_HUB_PORT
HUB_HTTP_HOST = "%s:%s" % (HOST, PORT)
STATIC_PAGE_NAMES = frozenset(("index.html", "build.html", "desk.js", "desk.css"))
ROOT = Path(__file__).resolve().parent
HUB_PROCESS = ROOT / ".hub-process"
BUS = ROOT / "bus"
HANDOFF = Path(os.environ.get("HBC_HANDOFF") or (Path.home() / ".grok" / "handoff"))
LOG = ROOT / "approvals.jsonl"
SEEN = ROOT / ".seen-ids.txt"
GROK = Path.home() / ".grok" / "bin" / "grok.exe"

def default_build_cwd(root=None, environ=None):
    """Neutral build folder: %LOCALAPPDATA%\\LPIN\\build, else <app>/build-work.

    Never drive-root\\AOS. HBC_BUILD_CWD overrides when set.
    """
    env = os.environ if environ is None else environ
    override = str(env.get("HBC_BUILD_CWD") or "").strip()
    if override:
        return override
    local = str(env.get("LOCALAPPDATA") or "").strip()
    if local:
        return str(Path(local) / "LPIN" / "build")
    base = Path(root) if root is not None else ROOT
    return str(Path(base) / "build-work")


BUILD_CWD = default_build_cwd()
_LANES = lanes.load()
OWNERS = dict(_LANES.owners)
RETURNS = dict(_LANES.returns)
FIELDS = ("id", "from", "to", "lane", "priority", "job", "done-when")
# #6 auto-reopen stays behind this flag. It defaults off and is not
# turned on here. Leftover CLAIMED ids are never auto-run.
HBC_BUILD_AUTOREOPEN = False
NO_GO_NO_ACTION = "no GO, no action"
CLAIM_HELPER = "tools/Claim-BusJob.ps1"
CLAIM_EXIT_GO = 0
CLAIM_EXIT_NOGO = 2
CLAIM_EXIT_ERROR = 3
WATCHER_PROMPT = lanes.watcher_prompt(_LANES)


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_preview_quiet = False


def record(row):
    if _preview_quiet:
        return
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


_seen_alarm_state = None
_seen_claim_alarmed = False


def _flush_seen_events():
    had_claims = False
    for event in protocol.take_seen_events():
        kind = event.get("kind", "")
        if kind == "prune":
            for job_id in event.get("ids", []):
                record({
                    "time": now(),
                    "id": job_id,
                    "decision": "prune",
                    "reason": "SEEN_RETENTION",
                })
            print("seen-list prune", flush=True)
        elif kind == "migrate":
            record({
                "time": now(),
                "id": "",
                "decision": "migrate",
                "reason": "SEEN_LIST",
            })
        elif kind == "unfinished_claim":
            had_claims = True
            _note_claim_alarm(event.get("ids", []))
    return had_claims


def _note_seen_ok():
    global _seen_alarm_state
    _seen_alarm_state = "ok"


def _note_seen_alarm():
    """Alarm once per state change. /api/state polls must not append a new row each time."""
    global _seen_alarm_state
    if _seen_alarm_state == "alarm":
        return
    _seen_alarm_state = "alarm"
    record({
        "time": now(),
        "id": "",
        "decision": "alarm",
        "reason": "SEEN_LIST",
    })
    print("seen-list alarm", flush=True)


def _note_claim_alarm(ids):
    """SEEN_CLAIM once per state change, not on every leftover-claim poll."""
    global _seen_claim_alarmed
    if _seen_claim_alarmed:
        return
    _seen_claim_alarmed = True
    job_id = ""
    for item in ids or []:
        if item:
            job_id = item
            break
    record({
        "time": now(),
        "id": job_id,
        "decision": "alarm",
        "reason": "SEEN_CLAIM",
    })


def _note_return_alarm(job_id=""):
    """Return-file swap failed after the run was already recorded."""
    record({
        "time": now(),
        "id": job_id,
        "decision": "alarm",
        "reason": "RETURN",
    })
    print("return-file alarm", flush=True)


def seen_ids(alarm_claims=True):
    global _seen_claim_alarmed
    protocol.set_approvals_log(LOG)
    try:
        ids = protocol.seen_ids(SEEN, alarm_claims=alarm_claims)
        had_claims = _flush_seen_events()
        if alarm_claims and not had_claims:
            _seen_claim_alarmed = False
            _note_seen_ok()
        return ids
    except protocol.SeenListError:
        _note_seen_alarm()
        raise


def mark_seen(job_id, fields=None, verdict=None):
    if fields is None:
        return
    protocol.record_seen(SEEN, fields, verdict)


BUS_READ_MAX_BYTES = 1024 * 1024
SOURCE_BUS = "bus"
SOURCE_HANDOFF = "handoff"
SOURCE_ARCHIVE = "archive"
SOURCE_SHADOW = "shadow"
_shadow_alarmed = set()
_bus_read_alarmed = set()


def read_bus(path):
    """Read a bus file as bytes, then decode. Lone CR is kept, matching the signer."""
    path = Path(path)
    try:
        with path.open("rb") as handle:
            raw = handle.read(BUS_READ_MAX_BYTES + 1)
    except OSError:
        return ""
    if len(raw) > BUS_READ_MAX_BYTES:
        _note_bus_read_alarm(path)
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def blocks(text):
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
            chunk = lines[start:index]
            fields = parse_block_lines(chunk)
            if fields:
                fields["_raw_lines"] = chunk
                found.append(fields)
        else:
            index += 1
    return found


JOB_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


def job_id_reason(fields=None):
    """Ids are ASCII [A-Za-z0-9._:-] only. Homoglyphs and spaces are BAD_ID."""
    job_id = str((fields or {}).get("id") or "")
    if not job_id or not JOB_ID_RE.fullmatch(job_id):
        return "BAD_ID"
    return ""


def check(fields, owner):
    bad_id = job_id_reason(fields)
    if bad_id:
        return bad_id
    missing = protocol.missing_field_reason(fields)
    if missing:
        return missing
    pri = protocol.priority_reason(fields)
    if pri:
        return pri
    owned = protocol.owner_reason(fields, owner)
    if owned:
        return owned
    if fields.get("id") in seen_ids():
        return "DUPLICATE"
    verb = protocol.verb_reason(fields)
    if verb:
        return verb
    if pane_reason(fields):
        return "BAD_PANE"
    mapped = resolve_pane(fields)
    if mapped not in ("1", "2"):
        return "UNMAPPED"
    return ""


def _handoff_log_name(path):
    parts = Path(path).parts
    if len(parts) >= 2:
        return "%s/%s" % (parts[-2], parts[-1])
    return Path(path).name or "handoff"


def default_live_handoff_dir():
    return Path.home() / ".grok" / "handoff"


def live_handoff_dir():
    """Default live handoff ~/.grok/handoff. Test seam, not a second config."""
    return default_live_handoff_dir()


def staging_handoff_dir(root=None):
    return Path(root if root is not None else ROOT) / "handoff-staging"


def _resolved_norm(path):
    """Resolve junctions/symlinks, then case-fold. Empty on failure."""
    if path is None:
        return ""
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except Exception:
        return ""


def _paths_same(left, right):
    a = _resolved_norm(left)
    b = _resolved_norm(right)
    return bool(a) and a == b


def is_live_handoff_path(path):
    """True when path is the default live handoff (~/.grok/handoff).

    Staging refuses that default path only. live_handoff_dir is the same
    folder (and the test seam). Resolves junctions, then compares
    case-insensitively.
    """
    if path is None:
        return False
    return _paths_same(path, live_handoff_dir()) or _paths_same(path, default_live_handoff_dir())


def staging_flag(environ=None):
    source = os.environ if environ is None else environ
    return str(source.get("HBC_STAGING") or "").strip() == "1"


def staging_marker(root=None):
    try:
        return (Path(root if root is not None else ROOT) / ".hbc-staging").is_file()
    except Exception:
        return False


def is_staging(environ=None, root=None, port=None):
    """True only when HBC_STAGING=1 and ROOT has .hbc-staging. Port never decides."""
    del port  # port is never a staging signal
    return staging_flag(environ) and staging_marker(root)


def warn_or_refuse_staging(environ=None, root=None):
    """Marker-only is LIVE-safe. HBC_STAGING=1 without the marker refuses to start."""
    flag = staging_flag(environ)
    marker = staging_marker(root)
    if flag and not marker:
        print("WARNING: HBC_STAGING=1 without .hbc-staging; refuse to start", flush=True)
        try:
            log_hub_event("staging-refuse", reason="missing-marker")
        except Exception:
            pass
        raise SystemExit("HBC_STAGING=1 requires .hbc-staging")
    if marker and not flag:
        print("WARNING: .hbc-staging present without HBC_STAGING=1; treating as LIVE", flush=True)
        try:
            log_hub_event("staging-warn", reason="marker-only")
        except Exception:
            pass


def _under_root(path, root):
    """True when resolved path is ROOT or inside it. Case-insensitive."""
    child = _resolved_norm(path)
    parent = _resolved_norm(root)
    if not child or not parent:
        return False
    if child == parent:
        return True
    sep = os.sep
    return child.startswith(parent + sep)


def watcher_boot_allowed(environ=None, root=None, port=None):
    """Staging never starts the live watcher. Live handoff on staging is refused."""
    if is_staging(environ=environ, root=root, port=port):
        return False
    return True


def _handoff_path_ok(raw):
    """Absolute folder only. Refuse UNC and reparse points. Returns (ok, path_or_reason)."""
    text = str(raw or "").strip().strip('"')
    if not text:
        return False, "empty"
    if _is_unc_or_device_path(text):
        return False, "unc"
    if os.name == "nt" and _is_rooted_no_drive(text):
        return False, "relative"
    path = Path(text)
    if not path.is_absolute():
        return False, "relative"
    if _is_link(path):
        return False, "reparse"
    try:
        resolved = path.resolve()
    except OSError:
        return False, "resolve"
    if _is_unc_or_device_path(str(resolved)):
        return False, "unc"
    if _is_link(resolved):
        return False, "reparse"
    try:
        if resolved.exists() and not resolved.is_dir():
            return False, "not-dir"
    except OSError:
        return False, "resolve"
    return True, resolved


def configure_handoff(raw=None, environ=None, root=None):
    """Resolve and install HANDOFF. Invalid configs disable handoff reads."""
    global HANDOFF
    source = os.environ if environ is None else environ
    base = Path(root if root is not None else ROOT)
    staging = is_staging(environ=source, root=base)
    if raw is None:
        env = str(source.get("HBC_HANDOFF") or "").strip()
        if staging:
            raw = staging_handoff_dir(base)
            if env:
                ok_env, env_path = _handoff_path_ok(env)
                if ok_env and _under_root(env_path, base) and not is_live_handoff_path(env_path):
                    raw = env_path
        else:
            raw = env if env else HANDOFF
    ok, value = _handoff_path_ok(raw)
    if ok and staging and is_live_handoff_path(value):
        ok, value = False, "live-handoff"
    if not ok:
        HANDOFF = None
        print("handoff folder refused: %s" % value, flush=True)
        try:
            log_hub_event("handoff-dir", name="handoff", ok=0, reason=value)
        except Exception:
            pass
        return None
    HANDOFF = Path(value)
    print("handoff folder: %s" % _handoff_log_name(HANDOFF), flush=True)
    try:
        log_hub_event("handoff-dir", name=_handoff_log_name(HANDOFF), ok=1)
    except Exception:
        pass
    return HANDOFF


def handoff_dir():
    """Configured handoff folder. HBC_HANDOFF, else the profile .grok/handoff."""
    if not HANDOFF:
        return None
    return Path(HANDOFF)


def _to_build_files(root):
    if root is None:
        return
    root = Path(root)
    try:
        if not root.is_dir():
            return
    except OSError:
        return
    for name, owner in OWNERS.items():
        path = root / name
        if path.is_file() and not _is_link(path):
            yield path, owner


def _note_shadow_alarm(job_id):
    job_id = str(job_id or "")
    if not job_id or job_id in _shadow_alarmed:
        return
    _shadow_alarmed.add(job_id)
    record({
        "time": now(),
        "id": job_id,
        "decision": "alarm",
        "reason": "SHADOW",
    })
    print("job-id shadow", flush=True)
    try:
        log_hub_event("job-shadow", name=job_id[:80])
    except Exception:
        pass


def _note_bus_read_alarm(path):
    name = Path(path).name
    if name in _bus_read_alarmed:
        return
    _bus_read_alarmed.add(name)
    print("bus file ignored: oversized", flush=True)
    record({
        "time": now(),
        "id": "",
        "decision": "alarm",
        "reason": "BUS_READ",
    })
    try:
        log_hub_event("bus-read", name=name, reason="oversized")
    except Exception:
        pass


def _parse_to_build(root):
    found = []
    for path, owner in _to_build_files(root):
        try:
            parsed = blocks(read_bus(path))
        except Exception:
            continue
        for fields in parsed:
            job_id = str(fields.get("id") or "")
            if job_id:
                found.append((job_id, fields, owner))
    return found


def _live_job_index():
    """id -> {bus: (fields, owner)|None, handoff: (fields, owner)|None}."""
    index = {}
    for job_id, fields, owner in _parse_to_build(BUS):
        slot = index.setdefault(job_id, {"bus": None, "handoff": None})
        if slot["bus"] is None:
            slot["bus"] = (fields, owner)
    for job_id, fields, owner in _parse_to_build(handoff_dir()):
        slot = index.setdefault(job_id, {"bus": None, "handoff": None})
        if slot["handoff"] is None:
            slot["handoff"] = (fields, owner)
    return index


def _resolve_live_job(job_id, index=None):
    """Shared lookup. Returns (fields, owner, source)."""
    index = _live_job_index() if index is None else index
    slot = index.get(str(job_id or ""))
    if not slot:
        return None, None, ""
    bus_hit = slot.get("bus")
    hand_hit = slot.get("handoff")
    if bus_hit and hand_hit:
        return bus_hit[0], bus_hit[1], SOURCE_SHADOW
    if bus_hit:
        return bus_hit[0], bus_hit[1], SOURCE_BUS
    if hand_hit:
        return hand_hit[0], hand_hit[1], SOURCE_HANDOFF
    return None, None, ""


def _queue_item(fields, owner, verdict):
    job_text = fields.get("job", "")
    return {
        "id": screen_safe(fields.get("id", "")),
        "owner": owner,
        "from": screen_safe(fields.get("from") or owner),
        "lane": screen_safe(fields.get("lane", "")),
        "pane": resolve_pane(fields) or fields.get("pane", ""),
        "priority": fields.get("priority", ""),
        "job": job_text,
        "summary": screen_safe(job_text),
        "hash": protocol.content_fingerprint(fields),
        "status": "ready" if verdict.ready else "held",
        "reason": verdict.queue_reason,
        "path": "watcher" if routes_to_watcher(fields) else "hub",
    }


def _evaluate_queue_job(fields, owner):
    try:
        verdict = evaluate(fields, owner)
    except protocol.SeenListError:
        _note_seen_alarm()
        verdict = protocol.Verdict("REFUSED", "SEEN_LIST", http="SEEN_LIST")
    except Exception:
        verdict = protocol.Verdict("REFUSED", "MALFORMED", http="MALFORMED")
    reason = verdict.queue_reason
    if reason in ("BAD_PANE", "UNMAPPED", "BAD_ID"):
        log_held(fields, reason, once=True)
    elif verdict.kind == "SPOOFED":
        log_spoofed(fields, verdict.reason, once=True)
    return verdict


def queue():
    items = []
    index = _live_job_index()
    for job_id in index:
        fields, owner, source = _resolve_live_job(job_id, index)
        if source == SOURCE_SHADOW:
            _note_shadow_alarm(job_id)
            item = _queue_item(
                fields,
                owner,
                protocol.Verdict("REFUSED", "SHADOW", http="SHADOW"),
            )
            item["status"] = "held"
            item["reason"] = "SHADOW"
            item["path"] = "shadow"
            items.append(item)
            continue
        if source == SOURCE_HANDOFF and not routes_to_watcher(fields):
            continue
        verdict = _evaluate_queue_job(fields, owner)
        items.append(_queue_item(fields, owner, verdict))
    items.sort(key=lambda item: item["priority"] != "urgent")
    return items


def _original_verdict(job_id):
    for name in RETURNS.values():
        path = BUS / name
        if not path.is_file():
            continue
        found = protocol.first_verdict_for_id(path.read_text(encoding="utf-8"), job_id)
        if found:
            return found
    return "[UNKNOWN]"


def evaluate(fields, owner):
    """Order: id, signature, P6/P5, time and age, routed, duplicate, stop, check(), run."""
    bad_id = job_id_reason(fields)
    if bad_id:
        return protocol.Verdict("REFUSED", bad_id, http=bad_id)
    seen_alarm = False
    seen = set()
    try:
        seen = seen_ids()
    except protocol.SeenListError:
        seen_alarm = True
    sig_reason = verify_job(fields, owner, seen=seen)
    if sig_reason == "REPLAY":
        try:
            rec = protocol.find_seen(SEEN, fields.get("id", ""))
        except protocol.SeenListError:
            rec = None
        original_id = (rec or {}).get("id") or str(fields.get("id", "") or "")
        if rec and protocol.is_claim(rec):
            return protocol.Verdict(
                "DUPLICATE",
                "",
                http="DUPLICATE",
                original_id=original_id,
                original_verdict=_original_verdict(original_id),
            )
        return protocol.Verdict(
            "DUPLICATE",
            "",
            http="SPOOFED REPLAY",
            original_id=original_id,
            original_verdict=_original_verdict(original_id),
        )
    if sig_reason:
        return protocol.Verdict("SPOOFED", sig_reason, http=f"SPOOFED {sig_reason}")
    if seen_alarm:
        return protocol.Verdict("REFUSED", "SEEN_LIST", http="SEEN_LIST")

    span = protocol.field_span_reason(fields)
    if span:
        return protocol.Verdict("REFUSED", span, http=span)

    pri = protocol.priority_reason(fields)
    if pri:
        return protocol.Verdict("REFUSED", pri, http=pri)

    missing = protocol.missing_field_reason(fields)
    if missing:
        return protocol.Verdict("REFUSED", missing, http=missing)

    owned = protocol.owner_reason(fields, owner)
    if owned:
        return protocol.Verdict("SPOOFED", owned, http="SPOOFED %s" % owned)

    clock = protocol.job_time_reason(fields)
    if clock:
        return protocol.Verdict("REFUSED", clock, http=clock)

    routed = protocol.routed_reason(fields)
    if routed:
        return protocol.Verdict("REFUSED", routed, http=routed)

    digest = protocol.content_fingerprint(fields)
    try:
        same = protocol.find_content(SEEN, digest)
    except protocol.SeenListError:
        _note_seen_alarm()
        return protocol.Verdict("REFUSED", "SEEN_LIST", http="SEEN_LIST")
    if same:
        original_id = same.get("id", "")
        return protocol.Verdict(
            "DUPLICATE",
            "SAME_CONTENT",
            http="DUPLICATE SAME_CONTENT",
            original_id=original_id,
            original_verdict=_original_verdict(original_id),
        )

    stop = protocol.stop_reason(fields)
    if stop == "STOPPED":
        return protocol.Verdict("STOPPED", "", http="STOPPED")
    if stop:
        return protocol.Verdict("REFUSED", stop, http=stop)

    reason = check(fields, owner)
    if not reason:
        return protocol.Verdict("OK", ready=True, http="")
    if reason == "DUPLICATE":
        try:
            rec = protocol.find_seen(SEEN, fields.get("id", ""))
        except protocol.SeenListError:
            rec = None
        original_id = (rec or {}).get("id") or str(fields.get("id", "") or "")
        return protocol.Verdict(
            "DUPLICATE",
            "",
            http="DUPLICATE",
            original_id=original_id,
            original_verdict=_original_verdict(original_id),
        )
    if reason == "SPOOFED":
        return protocol.Verdict("SPOOFED", "", http="SPOOFED")
    if reason == "OWNER_MISMATCH":
        return protocol.Verdict("SPOOFED", reason, http="SPOOFED %s" % reason)
    if reason in (
        "BAD_PANE",
        "UNMAPPED",
        "BAD_ID",
        "BAD_PRIORITY",
        "BAD_VERB",
        "MISSING_FIELD",
        "BAD_FIELD",
        "VERBS_UNAVAILABLE",
    ):
        return protocol.Verdict("REFUSED", reason, http=reason)
    if reason.startswith("MALFORMED"):
        return protocol.Verdict("REFUSED", "MALFORMED", http=reason)
    if reason.startswith("REFUSED"):
        return protocol.Verdict("REFUSED", "", http=reason)
    return protocol.Verdict("REFUSED", "", http=reason)


def _job_id_matches(fields, job_id):
    return fields.get("id") == job_id


def find_job(job_id):
    """Return (fields, owner, source). Source is bus, handoff, archive, or shadow."""
    job_id = str(job_id or "")
    fields, owner, source = _resolve_live_job(job_id)
    if fields:
        return fields, owner, source
    for name, owner in OWNERS.items():
        stem = name[:-3] if name.endswith(".md") else name
        for arch in sorted(BUS.glob(stem + "-ARCHIVE-*.md")):
            try:
                parsed = blocks(read_bus(arch))
            except Exception:
                continue
            for fields in parsed:
                if _job_id_matches(fields, job_id):
                    return fields, owner, SOURCE_ARCHIVE
    return None, None, ""


def _pending_path(owner):
    path = BUS / RETURNS[owner]
    return path.with_name(path.name + ".pending.json")


def _atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _os_replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_pending_file(path):
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if item is not None]


def _write_pending_file(path, items):
    if not items:
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
        return
    _atomic_write(path, json.dumps(list(items), ensure_ascii=False))


def clear_pending_returns():
    for owner in RETURNS:
        path = _pending_path(owner)
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass


def load_pending_returns():
    """Load the on-disk reply queue. Disk is the source of truth after restart."""
    loaded = {}
    for owner in RETURNS:
        loaded[owner] = _read_pending_file(_pending_path(owner))
    return loaded


def append_return(owner, text):
    path = BUS / RETURNS[owner]
    pending = _pending_path(owner)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_text = str(text or "")
    persisted = False
    try:
        with protocol.lock_bus(path):
            parts = _read_pending_file(pending) + [new_text]
            try:
                current = path.read_text(encoding="utf-8") if path.is_file() else ""
                extra = current
                for part in parts:
                    extra += "\n" + part + "\n"
                _atomic_write(path, extra)
                _write_pending_file(pending, [])
            except Exception:
                _write_pending_file(pending, parts)
                persisted = True
                raise
    except Exception:
        if not persisted:
            try:
                with protocol.lock_bus(path):
                    parts = _read_pending_file(pending) + [new_text]
                    _write_pending_file(pending, parts)
            except Exception:
                pass
        raise
    job_id = ""
    for line in str(text or "").splitlines():
        if line.startswith("id: "):
            job_id = line[4:].strip()
            break
    if job_id:
        try:
            fields, _found, _source = find_job(job_id)
            if fields:
                archive_job(fields)
        except Exception:
            pass


TAIL_MAX_CHARS = 2000
TAIL_MAX_LINES = 40
SCRUB_PRE_CAP = 8192
TRUNC_MARK = "\n<truncated>"
# Bounded classes only. No nested quantifiers. Cap input before matching.
_SEP = r"(?:[\\/]|%5[Cc]|%2[Ff])"
_NAME = r"[^\s\\/%]{1,64}"
_USERS_DIR = re.compile(rf"(?i)(?:[A-Za-z]:|{_SEP}{{2}}{_NAME})?{_SEP}Users{_SEP}{_NAME}")
_POSIX_HOME = re.compile(r"/home/[^\s/]{1,64}")
_DRIVE_ROOT = re.compile(r"^[A-Za-z]:$")
_EXACT_PLACEHOLDERS = ("<home>", "<user>", "<env>", "<truncated>")
_EXACT_PLACEHOLDER_RE = re.compile("|".join(re.escape(item) for item in _EXACT_PLACEHOLDERS))
_HARMLESS_VALUE = re.compile(r"^[A-Za-z0-9._\- ]{1,48}$")
NAME_WORD_MIN = 3
GENERIC_USERNAMES = frozenset({
    "test",
    "user",
    "admin",
    "administrator",
    "bin",
    "guest",
    "owner",
})
_ENV_SKIP_KEYS = frozenset({
    "OS",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "NUMBER_OF_PROCESSORS",
    "TERM",
    "PROMPT",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL",
    "PROCESSOR_REVISION",
    "PROCESSOR_ARCHITEW6432",
})


def _replace_ci(text, needle, repl):
    if not text or not needle:
        return text
    return re.compile(re.escape(needle), re.IGNORECASE).sub(repl, text)


def _username_word_ok(name):
    """True when name is a whole-word scrub. Generics stay path-segment only."""
    return len(name) >= NAME_WORD_MIN and name.casefold() not in GENERIC_USERNAMES


def _replace_username(text, name):
    """Replace name on word or path boundaries. Placeholders must already be stashed.

    Names of 3 or more characters are whole words (word boundaries,
    case-insensitive) unless the name is a generic account (test, user,
    admin, administrator, bin, guest, owner). Shorter names stay
    path-segment only so one- and two-letter words are not rewritten.
    Generic names match only as a path segment directly under Users or
    home, so \\Git\\bin\\git.exe and /usr/bin/env are left alone.
    """
    if not text or not name:
        return text
    escaped = re.escape(name)
    if len(name) < NAME_WORD_MIN:
        pat = re.compile(
            r"(?:(?<=[\\/])|(?<=%5[Cc])|(?<=%2[Ff]))" + escaped + r"(?:(?=[\\/])|(?=%5[Cc])|(?=%2[Ff])|$)",
            re.IGNORECASE,
        )
    elif name.casefold() in GENERIC_USERNAMES:
        pat = re.compile(
            r"(?:(?<=[\\/]Users[\\/])|(?<=[\\/]home[\\/])"
            r"|(?<=%5[Cc]Users%5[Cc])|(?<=%5[Cc]home%5[Cc])"
            r"|(?<=%2[Ff]Users%2[Ff])|(?<=%2[Ff]home%2[Ff]))"
            + escaped
            + r"(?:(?=[\\/])|(?=%5[Cc])|(?=%2[Ff])|$)",
            re.IGNORECASE,
        )
    else:
        pat = re.compile(r"(?<![A-Za-z0-9_])" + escaped + r"(?![A-Za-z0-9_])", re.IGNORECASE)
    return pat.sub("<user>", text)


def _path_variants(value):
    items = set()
    if not value:
        return []
    items.add(value)
    items.add(value.replace("\\", "/"))
    items.add(value.replace("/", "\\"))
    items.add(value.replace("\\", "\\\\"))
    items.add(value.replace("\\", "%5C").replace("/", "%5C"))
    items.add(value.replace("\\", "%5c").replace("/", "%5c"))
    items.add(value.replace("\\", "%2F").replace("/", "%2F"))
    items.add(value.replace("\\", "%2f").replace("/", "%2f"))
    items.add(value.replace("\\", "%5C").replace("/", "%2F"))
    items.add(value.replace("\\", "%5c").replace("/", "%2f"))
    return sorted((item for item in items if item), key=len, reverse=True)


def _parent_dir(path):
    trimmed = path.rstrip("\\/")
    if "\\" in trimmed:
        parent = trimmed.rsplit("\\", 1)[0]
    elif "/" in trimmed:
        parent = trimmed.rsplit("/", 1)[0]
    else:
        return ""
    if parent in (".", "/", "\\") or _DRIVE_ROOT.fullmatch(parent):
        return ""
    return parent


def _stash_placeholders(text, held, token):
    def repl(match):
        held.append(match.group(0))
        return "%s%d%s" % (token, len(held) - 1, token)
    return _EXACT_PLACEHOLDER_RE.sub(repl, text)


def _unstash_placeholders(text, held, token):
    for index, placeholder in enumerate(held):
        text = text.replace("%s%d%s" % (token, index, token), placeholder)
    return text


def _looks_harmless(value):
    if len(value) < 6:
        return True
    if re.search(r"(?i)sk-[A-Za-z0-9]", value):
        return False
    if "<" in value or ">" in value:
        return False
    if len(value) > 48:
        return False
    return bool(_HARMLESS_VALUE.fullmatch(value))


def _skip_env_value(key, value):
    return key in _ENV_SKIP_KEYS and _looks_harmless(value)


def _full_homes():
    homes = []
    try:
        homes.append(str(Path.home()))
    except Exception:
        pass
    for key in ("USERPROFILE", "HOME"):
        value = os.environ.get(key) or ""
        if value:
            homes.append(value)
    return sorted({item for item in homes if item}, key=len, reverse=True)


def _usernames():
    names = []
    for key in ("USERNAME", "USER"):
        value = os.environ.get(key) or ""
        if value:
            names.append(value)
    try:
        import getpass
        name = getpass.getuser()
        if name:
            names.append(name)
    except Exception:
        pass
    return sorted({item for item in names if item}, key=len, reverse=True)


_BIDI_RE = re.compile("[\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c]")
_ZW_RE = re.compile("[\u200b-\u200d\u2060\ufeff]")
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_C0_RE = re.compile(r"[\x00-\x1f\x7f]")
_C1_RE = re.compile(r"[\x80-\x9f]")
_ID_CONTROL_RE = re.compile(
    "[\x00-\x1f\x7f\x80-\x9f"
    "\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c"
    "\u200b-\u200d\u2060\ufeff]"
)


def _strip_screen_controls(text):
    out = "" if text is None else str(text)
    if not out:
        return ""
    out = _ANSI_RE.sub("", out)
    out = _BIDI_RE.sub("", out)
    out = _ZW_RE.sub("", out)
    out = _C0_RE.sub("", out)
    return _C1_RE.sub("", out)


def id_reason(fields):
    """Same as job_id_reason: ASCII [A-Za-z0-9._:-] only. Controls and lookalikes are BAD_ID."""
    return job_id_reason(fields)


def screen_safe(text, limit=160):
    """Short scrubbed summary for the Build approvals panel. Never log the raw text."""
    cleaned = _strip_screen_controls(text)
    cleaned = scrub_tail(cleaned)
    cleaned = _strip_screen_controls(cleaned)
    if not cleaned:
        return ""
    cleaned = " ".join(cleaned.split())
    if limit > 0 and len(cleaned) > limit:
        return cleaned[:limit] + "..."
    return cleaned


def scrub_tail(text):
    """Scrub a child output tail before it is stored or returned. Never log the raw tail."""
    if text is None:
        return ""
    out = str(text)
    if not out:
        return ""
    had_mark = out.endswith(TRUNC_MARK)
    if had_mark:
        out = out[:-len(TRUNC_MARK)]
    truncated = False
    if len(out) > SCRUB_PRE_CAP:
        out = out[-SCRUB_PRE_CAP:]
        truncated = True

    held = []
    token = secrets.token_hex(8)
    out = _stash_placeholders(out, held, token)

    for home in _full_homes():
        for variant in _path_variants(home):
            out = _replace_ci(out, variant, "<home>")
    out = _stash_placeholders(out, held, token)

    for name in _usernames():
        out = _replace_username(out, name)
    out = _stash_placeholders(out, held, token)

    out = _USERS_DIR.sub("<home>", out)
    out = _stash_placeholders(out, held, token)

    profile_parent = _parent_dir(os.environ.get("USERPROFILE") or "")
    if profile_parent:
        for variant in _path_variants(profile_parent):
            out = _replace_ci(out, variant, "<home>")
        out = _stash_placeholders(out, held, token)

    out = _POSIX_HOME.sub("<home>", out)
    out = _stash_placeholders(out, held, token)

    env_vals = []
    for key, value in os.environ.items():
        if not value or len(value) < 6:
            continue
        if _skip_env_value(key, value):
            continue
        env_vals.append(value)
    for value in sorted(set(env_vals), key=len, reverse=True):
        out = out.replace(value, "<env>")
    out = _unstash_placeholders(out, held, token)

    lines = out.splitlines()
    if len(lines) > TAIL_MAX_LINES:
        out = "\n".join(lines[-TAIL_MAX_LINES:])
        truncated = True
    if len(out) > TAIL_MAX_CHARS:
        out = out[-TAIL_MAX_CHARS:]
        truncated = True
    if truncated or had_mark:
        out = out + TRUNC_MARK
    return out


_run_impl = None
_test_grok_argv = False


def set_run_impl(fn):
    """Test seam for the pane runner. Verification still runs first."""
    global _run_impl
    _run_impl = fn


def set_test_grok_argv(enabled):
    """Test-only: wrap non-.exe GROK fakes on Windows. Production stays [GROK, ...]."""
    global _test_grok_argv
    _test_grok_argv = bool(enabled)


_logged_spoofed = set()


def log_refusal(fields, reason, decision, once=False):
    row = fields if isinstance(fields, dict) else {}
    token = (row.get("id", ""), reason, decision)
    if once:
        if token in _logged_spoofed:
            return
        _logged_spoofed.add(token)
    record({
        "time": now(),
        "id": row.get("id", ""),
        "from": row.get("from", ""),
        "decision": decision,
        "reason": reason,
    })


def log_spoofed(fields, reason, once=False):
    log_refusal(fields, reason, "SPOOFED", once=once)


def log_held(fields, reason, once=False):
    log_refusal(fields, reason, "held", once=once)


def pane_child_env(home):
    """Env for the pane grok child. Windows gets a short allowlist; nothing else."""
    env = {
        "GROK_HOME": str(home),
        "PATH": str(GROK.parent),
    }
    if os.name != "nt":
        return env
    for key in WIN_CHILD_ENV:
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _grok_argv(*args):
    """Production is always [GROK, ...]. Non-.exe wrapping is the test hook only."""
    grok = str(GROK)
    if not _test_grok_argv or os.name != "nt":
        return [grok, *args]
    ext = os.path.splitext(grok)[1].lower()
    if ext == ".exe":
        return [grok, *args]
    if ext in {".cmd", ".bat"}:
        return [os.environ.get("COMSPEC") or "cmd.exe", "/c", grok, *args]
    return [sys.executable, grok, *args]


def run_pane(pane, command, fields=None, owner=None):
    seen = seen_ids(alarm_claims=False)
    current = str((fields or {}).get("id", "") or "")
    if current:
        seen.discard(current)
    sig_reason = verify_job(fields or {}, owner or "", seen=seen)
    if sig_reason:
        log_spoofed(fields, sig_reason)
        return 403, "", sig_reason
    if pane_reason(fields):
        return 403, "", "BAD_PANE"
    expected = resolve_pane(fields or {})
    if not expected or str(pane) != expected:
        log_spoofed(fields, "PANE_MISMATCH")
        _drop_unfinished_claim(fields)
        return 403, "", "PANE_MISMATCH"
    if routes_to_watcher(fields):
        return 403, "", "WRONG_PATH"
    if fields:
        job_id = str(fields.get("id", "") or "")
        rec = None
        if job_id:
            try:
                rec = protocol.find_seen(SEEN, job_id, alarm_claims=False)
            except protocol.SeenListError:
                rec = None
        if rec:
            return 403, "", "DUPLICATE"
        try:
            _claim_run(fields)
        except protocol.SeenListError:
            _note_seen_alarm()
            return 403, "", "SEEN_LIST"
    if _run_impl is not None:
        code, out, err = _run_impl(pane, command)
        return code, scrub_tail(out), scrub_tail(err)
    home = ROOT / "panes" / f"pane-{pane}"
    home.mkdir(parents=True, exist_ok=True)
    if not GROK.is_file():
        return 127, "", "grok.exe not found"
    completed = subprocess.run(
        _grok_argv("-p", command, "--permission-mode", "plan", "--max-turns", "1",
         "--cwd", str(home), "--disable-web-search"),
        cwd=str(home),
        capture_output=True,
        text=True,
        timeout=120,
        env=pane_child_env(home),
    )
    return completed.returncode, scrub_tail(completed.stdout), scrub_tail(completed.stderr)


LINKS = {
    "x": "https://x.com/",
    "imagine": "https://grok.com/imagine",
    "grok": "https://grok.com/",
}


def bus_view():
    view = {}
    for path in sorted(BUS.glob("*.md")):
        view[path.name] = read_bus(path)[-4000:]
    return view


SESSIONS = ROOT / ".sessions.json"
EVENTS = ROOT / ".hub-events.jsonl"
SESSIONS_MAX_BYTES = 64 * 1024
EVENTS_MAX_BYTES = 64 * 1024
LAB_PANES = frozenset(("1", "2"))
BUILD_PANES = frozenset(("build",) + tuple(f"build{i}" for i in range(2, 9)))
BUS_WATCH_PROMPT = WATCHER_PROMPT
PANES = {}
_exit_logged = {}
_spawn_impl = None
_sessions_lock = threading.Lock()
_events_lock = threading.Lock()
_sessions_broken = False
# Required/optional is documentation only. Both tuples are searched the
# same way with os.path.isfile. A missing name never fails the allowlist.
PATH_REQUIRED_TOOLS = ("git.exe", "grok.exe")
PATH_OPTIONAL_TOOLS = ("wmic.exe",)
PATH_TOOL_NAMES = PATH_REQUIRED_TOOLS + PATH_OPTIONAL_TOOLS


def set_spawn_impl(fn):
    """Test seam for the pane PTY spawner."""
    global _spawn_impl
    _spawn_impl = fn


def valid_build_pane(pane):
    return pane in BUILD_PANES


def valid_term_pane(pane):
    return pane in LAB_PANES or valid_build_pane(pane)


def pane_boot_command(pane, environ=None):
    if pane == "build":
        if not watcher_boot_allowed(environ=environ):
            return ""
        return "grok --continue \"" + BUS_WATCH_PROMPT + "\"\r\n"
    if valid_build_pane(pane) or str(pane).startswith("build"):
        return "grok\r\n"
    return ""


def build_autoreopen_enabled(environ=None):
    source = os.environ if environ is None else environ
    return _truthy_flag(source.get("HBC_BUILD_AUTOREOPEN"))


def _exit_from_handle(handle):
    """Read an exit code from a process handle. Never look up a pid."""
    if handle is None:
        return None
    poll = getattr(handle, "poll", None)
    if callable(poll):
        value = poll()
        if isinstance(value, int):
            return value
    code = getattr(handle, "returncode", None)
    if isinstance(code, int):
        return code
    return None


def pane_exit_code(pty, fallback=None):
    """Exit code from the process handle. Unknown is None (logged as null)."""
    if isinstance(fallback, int):
        return fallback
    code = getattr(pty, "returncode", None)
    if isinstance(code, int):
        return code
    getter = getattr(pty, "get_exitstatus", None)
    if callable(getter):
        value = getter()
        if isinstance(value, int):
            return value
    status = getattr(pty, "exitstatus", None)
    if isinstance(status, int):
        return status
    for name in ("proc", "process", "_proc"):
        got = _exit_from_handle(getattr(pty, name, None))
        if isinstance(got, int):
            return got
    return None


def _rotate_events():
    if not EVENTS.is_file() or EVENTS.stat().st_size < EVENTS_MAX_BYTES:
        return
    first = EVENTS.with_name(EVENTS.name + ".1")
    second = EVENTS.with_name(EVENTS.name + ".2")
    if second.exists():
        second.unlink()
    if first.exists():
        os.replace(first, second)
    os.replace(EVENTS, first)


def log_hub_event(event, **fields):
    """Append one local hub event. No secrets. No absolute paths."""
    if _preview_quiet:
        return
    row = {"time": now(), "event": event}
    for key, value in fields.items():
        if key in ("path", "cwd", "file", "home"):
            continue
        row[key] = value
    line = json.dumps(row) + "\n"
    with _events_lock:
        _rotate_events()
        with EVENTS.open("a", encoding="utf-8") as handle:
            handle.write(line)


def _empty_store():
    return {"sessions": [], "open_build": []}


def _decode_sessions_bytes(raw):
    global _sessions_broken
    if len(raw) > SESSIONS_MAX_BYTES:
        print("sessions file ignored: oversized", flush=True)
        _sessions_broken = True
        return _empty_store()
    try:
        text = raw.decode("utf-8")
        data = json.loads(text)
    except UnicodeDecodeError:
        print("sessions file ignored: UnicodeDecodeError", flush=True)
        _sessions_broken = True
        return _empty_store()
    except json.JSONDecodeError:
        print("sessions file ignored: JSONDecodeError", flush=True)
        _sessions_broken = True
        return _empty_store()
    if isinstance(data, list):
        return {"sessions": data, "open_build": []}
    if not isinstance(data, dict):
        print("sessions file ignored: bad type", flush=True)
        _sessions_broken = True
        return _empty_store()
    jobs = data.get("sessions")
    if not isinstance(jobs, list):
        jobs = []
    open_build = data.get("open_build")
    if not isinstance(open_build, list):
        open_build = []
    names = []
    for name in open_build:
        pane = str(name)
        if pane and pane not in names:
            names.append(pane)
    return {"sessions": jobs, "open_build": names}


def _load_store():
    global _sessions_broken
    if not SESSIONS.is_file():
        return _empty_store()
    try:
        size = SESSIONS.stat().st_size
        if size > SESSIONS_MAX_BYTES:
            print("sessions file ignored: oversized", flush=True)
            _sessions_broken = True
            return _empty_store()
        raw = SESSIONS.read_bytes()
    except OSError:
        print("sessions file ignored: OSError", flush=True)
        _sessions_broken = True
        return _empty_store()
    return _decode_sessions_bytes(raw)


def _atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        if path.name == ".sessions.json":
            _maybe_restrict_secret_acl(tmp)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_store(store):
    if _sessions_broken:
        return
    _atomic_write_json(
        SESSIONS,
        {
            "sessions": store["sessions"][-20:],
            "open_build": list(store["open_build"]),
        },
    )


def sessions():
    with _sessions_lock:
        return _load_store()["sessions"]


def remember(pane, job_id, result):
    with _sessions_lock:
        store = _load_store()
        if _sessions_broken:
            return
        store["sessions"].append({"time": now(), "pane": pane, "id": job_id, "result": result[:240]})
        _write_store(store)


def remember_open_build(pane):
    if not valid_build_pane(pane):
        return
    with _sessions_lock:
        store = _load_store()
        if _sessions_broken:
            return
        if pane in store["open_build"]:
            return
        store["open_build"].append(pane)
        _write_store(store)


def forget_open_build(pane):
    with _sessions_lock:
        store = _load_store()
        if _sessions_broken:
            return
        if pane not in store["open_build"]:
            return
        store["open_build"] = [name for name in store["open_build"] if name != pane]
        _write_store(store)


def open_build_names():
    with _sessions_lock:
        return [name for name in _load_store()["open_build"] if valid_build_pane(name)]


def pane_alive(pane):
    pty = PANES.get(pane)
    return pty is not None and pty.isalive()


def spawn_pane(pane):
    home = ROOT / "panes" / f"pane-{pane}"
    home.mkdir(parents=True, exist_ok=True)
    child_env = term_child_env(home, pane)
    cwd = BUILD_CWD if str(pane).startswith("build") else str(home)
    if _spawn_impl is not None:
        return _spawn_impl(pane, child_env, cwd)
    if winpty is None:
        return None
    pty = winpty.PTY(120, 32)
    env_block = "\0".join(f"{key}={value}" for key, value in child_env.items()) + "\0\0"
    pty.spawn("cmd.exe", cmdline=None, cwd=cwd, env=env_block)
    return pty


def ensure_pane(pane, log="pane open"):
    """Open a pane if one is not already running. Persist Build sessions."""
    if valid_build_pane(pane) and is_staging():
        print("staging: skip pane %s" % pane, flush=True)
        try:
            log_hub_event("staging-no-pane", pane=pane)
        except Exception:
            pass
        return False
    if pane_alive(pane):
        return False
    pty = spawn_pane(pane)
    if pty is None:
        return False
    command = pane_boot_command(pane)
    if command:
        pty.write(command)
    attach_pane_identity(pty)
    PANES[pane] = pty
    _exit_logged.pop(pane, None)
    if valid_build_pane(pane):
        remember_open_build(pane)
    print(f"{log} {pane}", flush=True)
    return True


def note_pane_exit(pane, exit_code=None):
    if pane not in PANES:
        return
    pty = PANES.pop(pane)
    code = pane_exit_code(pty, fallback=exit_code)
    if valid_build_pane(pane) and not _exit_logged.get(pane):
        _exit_logged[pane] = True
        log_hub_event("build exit", pane=pane, exit=code)
    forget_open_build(pane)


def close_pane(pane):
    pty = PANES.get(pane)
    if pty is not None:
        closer = getattr(pty, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass
    note_pane_exit(pane)


def reopen_build_sessions(environ=None):
    if is_staging(environ):
        print("staging: skip auto-resume", flush=True)
        try:
            log_hub_event("staging-no-resume")
        except Exception:
            pass
        return
    if not build_autoreopen_enabled(environ):
        return
    seen = set()
    for pane in open_build_names():
        if pane in seen:
            continue
        seen.add(pane)
        ensure_pane(pane, log="pane reopen")


def after_port_bound():
    """Reopen recorded Build sessions only after the listen socket is up."""
    reopen_build_sessions()


def parse_hub_port(value, default=None):
    """Integer 1024-65535 from ASCII [0-9] only. Process-only; never setx."""
    if default is None:
        default = DEFAULT_HUB_PORT
    raw = "" if value is None else str(value).strip()
    if not raw:
        return default, None
    if not re.fullmatch(r"[0-9]+", raw):
        return default, "HBC_HUB_PORT %r refused; using %s" % (raw, default)
    try:
        port = int(raw, 10)
    except ValueError:
        return default, "HBC_HUB_PORT %r refused; using %s" % (raw, default)
    if port < HUB_PORT_MIN or port > HUB_PORT_MAX:
        return default, "HBC_HUB_PORT %s refused; using %s" % (port, default)
    return port, None


def apply_hub_port(value=None):
    """Set PORT and HUB_HTTP_HOST from process HBC_HUB_PORT. Never setx. 127.0.0.1-only."""
    global PORT, HUB_HTTP_HOST
    if value is None:
        value = os.environ.get("HBC_HUB_PORT", "")
    port, warning = parse_hub_port(value)
    PORT = port
    HUB_HTTP_HOST = "%s:%s" % (HOST, PORT)
    if warning:
        print("hub-port: %s" % warning, flush=True)
    return port


apply_hub_port()


def hub_host_ok(header):
    """Host must be exactly 127.0.0.1:<port>. The shell never uses localhost."""
    return str(header or "") == HUB_HTTP_HOST


@web.middleware
async def hub_host_middleware(request, handler):
    if not hub_host_ok(request.headers.get("Host", "")):
        return web.Response(status=421, text="refused")
    return await handler(request)


def _is_link(path):
    try:
        if path.is_symlink() or os.path.islink(str(path)):
            return True
        isjunc = getattr(os.path, "isjunction", None)
        if isjunc is not None and isjunc(str(path)):
            return True
    except OSError:
        return True
    return False


def requested_static_name(request):
    """Allowlisted page name, or None. Encoded/traversal/dot names are refused."""
    raw = str(getattr(request, "raw_path", "") or "").split("?", 1)[0]
    cooked = str(getattr(request, "path", "") or "")
    if "\\" in raw or "\\" in cooked or "%" in raw:
        return None
    if cooked == "/":
        return "index.html"
    name = cooked.lstrip("/")
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    if name.startswith("."):
        return None
    if name not in STATIC_PAGE_NAMES:
        return None
    return name


def resolve_static_page(name, root=None):
    """Resolve an allowlisted file under root. Refuse links and escapes."""
    if name not in STATIC_PAGE_NAMES:
        return None
    base = Path(root) if root is not None else ROOT
    path = base / name
    if _is_link(path):
        return None
    try:
        resolved = path.resolve()
        root_res = base.resolve()
    except OSError:
        return None
    if _is_link(resolved):
        return None
    if resolved.parent != root_res or resolved.name != name:
        return None
    if not resolved.is_file() or resolved.is_symlink():
        return None
    return resolved


async def status(request):
    """Read-only Control-tab probe. Loopback only. Does not touch signing or the bind."""
    return await handle_status(request)


async def restart_build(request):
    """Close the hub build pane only. Does not touch the hub process or signing."""
    if is_staging():
        return web.json_response({"ok": False, "error": "staging", "closed": []}, status=409)
    return await handle_restart_build(request)


def make_app():
    app = web.Application(middlewares=[hub_host_middleware])
    app["hbc_root"] = ROOT
    app["hbc_panes"] = PANES
    app["hbc_token"] = TOKEN
    app["hbc_port"] = PORT
    app["hbc_is_staging"] = is_staging
    app.router.add_get("/api/state", state)
    app.router.add_get("/term/{pane}", term_socket)
    app.router.add_post("/api/open", open_link)
    app.router.add_get("/api/job/{job_id}", job_preview)
    app.router.add_post("/api/decide", decide)
    app.router.add_post("/api/claim", claim)
    app.router.add_get("/status", status)
    app.router.add_post("/api/restart-build", restart_build)
    app.router.add_get("/", page)
    app.router.add_get("/{name}", page)
    return app


TOKEN = ""


def session_token_path():
    return ROOT / ".session-token"


def ensure_session_token():
    """Mint the HTTP session token. Called from start_hub, never at import."""
    global TOKEN
    TOKEN = secrets.token_hex(16)
    session_token_path().write_text(TOKEN, encoding="utf-8")


def secret_state_paths():
    """Token and sessions files under the current ROOT. Not the import-time SESSIONS path."""
    return (session_token_path(), Path(ROOT) / ".sessions.json")


_ACL_PATH_IN_TEXT = re.compile(
    r'(?i)(?:\\\\\?\\)?(?:[a-z]:\\|\\\\)[^\r\n:*?"<>|]+|(?<![A-Za-z0-9:])/[^\r\n:]+'
)
SYSTEM_SID = "S-1-5-18"


def _sid_ok(sid):
    sid = str(sid or "").strip()
    if not sid.startswith("S-") or sid.count("S") != 1:
        return False
    if any(ch in sid for ch in '<>"|:*? '):
        return False
    return all(ch.isdigit() or ch in "-S" for ch in sid)


def _current_user_sid():
    """SID string for the current process user. Empty when unavailable."""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        token_query = 0x0008
        token_user = 1

        class TOKEN_USER(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        get_current = kernel32.GetCurrentProcess
        get_current.restype = wintypes.HANDLE

        open_token = advapi32.OpenProcessToken
        open_token.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        open_token.restype = wintypes.BOOL

        get_info = advapi32.GetTokenInformation
        get_info.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        get_info.restype = wintypes.BOOL

        convert_sid = advapi32.ConvertSidToStringSidW
        convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        convert_sid.restype = wintypes.BOOL

        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        local_free = kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p

        token = wintypes.HANDLE()
        if not open_token(get_current(), token_query, ctypes.byref(token)):
            return ""
        try:
            needed = wintypes.DWORD(0)
            get_info(token, token_user, None, 0, ctypes.byref(needed))
            size = int(needed.value)
            if size <= 0:
                return ""
            buf = ctypes.create_string_buffer(size)
            if not get_info(token, token_user, buf, size, ctypes.byref(needed)):
                return ""
            info = ctypes.cast(buf, ctypes.POINTER(TOKEN_USER)).contents
            if not info.Sid:
                return ""
            sid_str = wintypes.LPWSTR()
            if not convert_sid(info.Sid, ctypes.byref(sid_str)):
                return ""
            try:
                return sid_str.value or ""
            finally:
                if sid_str:
                    local_free(sid_str)
        finally:
            close_handle(token)
    except Exception:
        return ""


def _scrub_acl_text(text, name="", path=None):
    """Strip filesystem paths from icacls stderr before hub events."""
    text = str(text or "")
    label = name or "<path>"
    seen = []
    if path is not None:
        seen.extend((str(path), os.fspath(path)))
        try:
            seen.append(str(Path(path).resolve()))
        except Exception:
            pass
    for item in seen:
        if item:
            text = text.replace(item, label)
    return _ACL_PATH_IN_TEXT.sub(label, text)


def _warn_secret_acl(name, err, path=None):
    text = _scrub_acl_text(err, name=name, path=path)[:80]
    print("secret-acl warning: %s %s" % (name, text), flush=True)
    try:
        log_hub_event("secret-acl-warn", name=name, error=text)
    except Exception:
        pass


def _restrict_one_secret_acl(path):
    path = Path(path)
    if not path.is_file():
        return
    sid = (_current_user_sid() or "").strip()
    if not _sid_ok(sid):
        _warn_secret_acl(path.name, "missing sid", path=path)
        return
    created = subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            # Locale-stable SIDs: Authenticated Users, Users, Everyone.
            "/remove",
            "*S-1-5-11",
            "/remove",
            "*S-1-5-32-545",
            "/remove",
            "*S-1-1-0",
            "/grant:r",
            "*%s:(M)" % sid,
            "/grant:r",
            "*%s:(F)" % SYSTEM_SID,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if created.returncode != 0:
        _warn_secret_acl(path.name, created.stderr or created.stdout or "icacls failed", path=path)


def _maybe_restrict_secret_acl(path):
    try:
        if not _secret_acl_windows():
            return
        _restrict_one_secret_acl(path)
    except Exception as exc:
        _warn_secret_acl(Path(path).name, exc, path=path)


def _secret_acl_windows():
    return os.name == "nt"


def restrict_secret_state_acl():
    """If secret state files exist, lock ACL to this user SID and SYSTEM. Never raises."""
    try:
        if not _secret_acl_windows():
            return
        for path in secret_state_paths():
            try:
                _restrict_one_secret_acl(path)
            except Exception as exc:
                _warn_secret_acl(Path(path).name, exc, path=path)
    except Exception as exc:
        _warn_secret_acl("secret-state", exc)


def hub_process_path():
    return Path(HUB_PROCESS)


def _process_create_filetime():
    if os.name == "nt":
        return _win_create_filetime()
    return 116444736000000000 + int(time.time() * 10_000_000)


def _win_create_filetime():
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_current = kernel32.GetCurrentProcess
    get_current.restype = wintypes.HANDLE
    get_times = kernel32.GetProcessTimes
    get_times.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    get_times.restype = wintypes.BOOL
    created = FILETIME()
    exited = FILETIME()
    kernel = FILETIME()
    user = FILETIME()
    if not get_times(get_current(), ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
        return 116444736000000000 + int(time.time() * 10_000_000)
    return (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)


def write_hub_process():
    """Record this hub process. Called from start_hub, never at import."""
    text = "pid=%s\nctime=%s\n" % (os.getpid(), _process_create_filetime())
    _atomic_write(hub_process_path(), text)


def clear_hub_process():
    try:
        hub_process_path().unlink()
    except OSError:
        pass


async def start_hub():
    apply_hub_port()
    ignore_disable_settings()
    warn_or_refuse_staging()
    if is_staging():
        print("staging: no auto-resume; handoff isolated", flush=True)
        try:
            log_hub_event("staging", port=PORT)
        except Exception:
            pass
    configure_handoff()
    ensure_session_token()
    restrict_secret_state_acl()
    load_pending_returns()
    log_hub_event("hub restart")
    runner = web.AppRunner(make_app())
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    after_port_bound()
    write_hub_process()
    return runner


def allowed(request):
    origin = request.headers.get("Origin", "")
    if origin and origin != f"http://{HOST}:{PORT}":
        return False
    if request.path.startswith("/term/") or request.path.startswith("/api/"):
        if not TOKEN:
            return False
        supplied = request.query.get("token") or request.headers.get("X-Hbc-Token", "")
        if not isinstance(supplied, str) or len(supplied) != len(TOKEN):
            return False
        return hmac.compare_digest(supplied, TOKEN)
    return True


# Terminal-only extras. WIN_CHILD_ENV is imported from hub.win_child_env
# (same contents as PR #2). At merge: drop PR #2's inline tuple and keep
# the import. Do not keep two WIN_CHILD_ENV definitions.
TERM_EXTRA_ENV = (
    "PATHEXT",
    "USERNAME",
    "HOMEDRIVE",
    "HOMEPATH",
    "PROGRAMFILES",
    "ProgramData",
)
GROK_AUTH_KEYS = ("XAI_API_KEY", "GROK_API_KEY")


def _truthy_flag(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _norm_path_dir(path):
    return os.path.normcase(os.path.normpath(str(path)))


def _split_parent_path(value):
    """Split a parent PATH. On Windows, split on ';' only so drive letters stay intact."""
    if not value:
        return []
    parts = []
    chunks = value.split(";") if os.name == "nt" else value.replace(";", os.pathsep).split(os.pathsep)
    for chunk in chunks:
        chunk = chunk.strip().strip('"')
        if chunk:
            parts.append(chunk)
    return parts


def _is_unc_or_device_path(text):
    slashes = str(text).replace("/", "\\")
    return slashes.startswith("\\\\")


def _has_win_drive(text):
    return len(text) >= 2 and text[0].isalpha() and text[1] == ":"


def _is_rooted_no_drive(text):
    """Windows current-drive root such as \\Tools or /Tools. Not a drive path, not UNC."""
    slashes = str(text).replace("/", "\\")
    if slashes.startswith("\\\\"):
        return False
    if _has_win_drive(text):
        return False
    if not slashes.startswith("\\"):
        return False
    if os.name == "nt":
        return True
    rest = text[1:].replace("\\", "/").strip("/")
    if text.startswith("/") and "/" in rest:
        return False
    return True


def _is_windows_apps_dir(text):
    """True for ...\\Microsoft\\WindowsApps and any folder under it."""
    key = os.path.normpath(str(text)).replace("/", "\\").casefold()
    marker = "\\microsoft\\windowsapps"
    return key.endswith(marker) or (marker + "\\") in key


def _allowed_parent_path_dir(raw):
    text = str(raw or "").strip().strip('"')
    if not text or text in (".", ".."):
        return False
    if _is_unc_or_device_path(text):
        return False
    if _is_rooted_no_drive(text):
        return False
    if _is_windows_apps_dir(text):
        return False
    if not os.path.isabs(text):
        return False
    return True


def _safe_windows_root(text):
    """Absolute X:\\name Windows root. Not UNC, not C:Windows, not C:\\."""
    text = str(text or "").strip().strip('"')
    if not text:
        return False
    if _is_unc_or_device_path(text):
        return False
    if _is_rooted_no_drive(text):
        return False
    if len(text) < 4:
        return False
    if not text[0].isalpha() or text[1] != ":" or text[2] not in "\\/":
        return False
    return bool(text[3:])


def _windows_system_root(source):
    """SYSTEMROOT, else WINDIR, else empty. Each must be a safe drive path."""
    for key in ("SYSTEMROOT", "WINDIR"):
        raw = (source.get(key) or "").strip().strip('"')
        if not _safe_windows_root(raw):
            continue
        trimmed = raw.rstrip("\\/")
        if not _safe_windows_root(trimmed):
            continue
        return trimmed
    return ""


def parent_path_tool_dirs(environ):
    """Absolute parent PATH folders that contain a required or optional tool.

    Looks for git.exe, grok.exe, and wmic.exe with os.path.isfile.
    The required/optional split does not change behaviour: every name is
    looked up the same way, and a miss never fails the allowlist. python.exe
    is not a look-for name. Microsoft\\WindowsApps is never appended.
    """
    found = []
    seen = set()
    for raw in _split_parent_path(environ.get("PATH") or ""):
        if not _allowed_parent_path_dir(raw):
            continue
        folder = os.path.normpath(raw)
        if not _allowed_parent_path_dir(folder):
            continue
        key = os.path.normcase(folder)
        if key in seen:
            continue
        if not any(os.path.isfile(os.path.join(folder, name)) for name in PATH_TOOL_NAMES):
            continue
        seen.add(key)
        found.append(folder)
    return found


def _default_grok_home(source):
    """On-disk grok login under the profile when GROK_HOME is unset."""
    profile = source.get("USERPROFILE") or source.get("HOME") or ""
    if not profile:
        try:
            profile = str(Path.home())
        except Exception:
            return ""
    if "\\" in profile:
        return profile.rstrip("\\/") + "\\.grok"
    return str(Path(profile) / ".grok")


def _windows_term_path(source):
    """Allowlisted Windows PATH plus parent dirs that hold required or optional tools."""
    root = _windows_system_root(source)
    parts = [str(GROK.parent)]
    if root:
        parts.append(root + "\\System32")
        parts.append(root + "\\System32\\Wbem")
        parts.append(root + "\\System32\\WindowsPowerShell\\v1.0")
        parts.append(root)
    seen = {_norm_path_dir(part) for part in parts}
    for extra in parent_path_tool_dirs(source):
        key = _norm_path_dir(extra)
        if key in seen:
            continue
        seen.add(key)
        parts.append(extra)
    return ";".join(parts)


def term_child_env(home, pane="1", environ=None):
    """Interactive terminal child env. Minimal allowlist. Build keeps the real GROK_HOME."""
    source = os.environ if environ is None else environ
    is_build = str(pane).startswith("build")
    env = {
        "PATH": str(GROK.parent),
        "PROMPT": f"pane{pane}$G",
    }
    if is_build:
        real = source.get("GROK_HOME")
        env["GROK_HOME"] = real if real else _default_grok_home(source)
        if not env["GROK_HOME"]:
            env.pop("GROK_HOME")
        if _truthy_flag(source.get("HBC_PASS_GROK_AUTH")):
            for key in GROK_AUTH_KEYS:
                value = source.get(key)
                if value:
                    env[key] = value
    else:
        env["GROK_HOME"] = str(home)
    if os.name != "nt":
        return env
    for key in WIN_CHILD_ENV + TERM_EXTRA_ENV:
        value = source.get(key)
        if value:
            env[key] = value
    env["PATH"] = _windows_term_path(source)
    return env


async def term_socket(request):
    if not allowed(request):
        return web.Response(status=403, text="refused")
    pane = request.match_info["pane"]
    if not valid_term_pane(pane):
        return web.Response(status=404, text="unknown pane")
    pty = PANES.get(pane)
    if pty is None or not pty.isalive():
        ensure_pane(pane)
        pty = PANES.get(pane)
        if pty is None:
            return web.Response(status=503, text="pane unavailable")
    socket = web.WebSocketResponse()
    await socket.prepare(request)

    async def pump():
        while pty.isalive():
            try:
                chunk = await asyncio.to_thread(pty.read, 4096)
            except EOFError:
                break
            if chunk:
                await socket.send_str(chunk)
        await socket.close()
        if not pty.isalive():
            note_pane_exit(pane)

    task = asyncio.create_task(pump())
    try:
        async for message in socket:
            if message.type == web.WSMsgType.TEXT:
                data = message.data
                if data.startswith("\x00resize:"):
                    try:
                        cols, rows = (int(v) for v in data[8:].split(","))
                        if 20 <= cols <= 400 and 5 <= rows <= 200:
                            size = (cols, rows)
                            if getattr(pty, "hbc_size", None) != size:
                                pty.set_size(cols, rows)
                                pty.hbc_size = size
                    except Exception:
                        pass
                    continue
                pty.write(data)
    finally:
        task.cancel()
        if not pane_alive(pane):
            note_pane_exit(pane)
    return socket


async def page(request):
    name = requested_static_name(request)
    if name is None:
        return web.Response(status=404, text="no")
    path = resolve_static_page(name)
    if path is None:
        return web.Response(status=404, text="no")
    kind = {".html": "text/html", ".css": "text/css", ".js": "text/javascript"}[path.suffix]
    body = path.read_bytes()
    if path.name == "index.html":
        body = body.replace(b"{{TOKEN}}", TOKEN.encode("ascii"))
    return web.Response(body=body, content_type=kind, headers={"Cache-Control": "no-store"})


def state_payload():
    try:
        sync_returns()
    except Exception:
        pass
    return {
        "class": "pattern-lab",
        "live": False,
        "freebots": "off",
        "hivebrain_game_chat": "off",
        "queue": queue(),
        "sessions": sessions(),
        "log": LOG.read_text(encoding="utf-8") if LOG.is_file() else "",
        "bus": bus_view(),
        "panes": ["1", "2"],
        "needs_you": needs_you(),
        "autoreopen": build_autoreopen(),
        "staging": is_staging(),
    }


async def state(request):
    if not allowed(request):
        return web.json_response({"error": "refused"}, status=403)
    return web.json_response(state_payload())


async def open_link(request):
    if not allowed(request):
        return web.json_response({"error": "refused"}, status=403)
    body = await request.json()
    name = body.get("name", "")
    if name == "grokbot":
        link = Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Grok Bot.lnk"
        if not link.is_file():
            return web.json_response({"error": "Grok Bot shortcut not found"}, status=404)
        subprocess.Popen(["cmd", "/c", "start", "", str(link)])
        return web.json_response({"opened": "grokbot"})
    url = LINKS.get(name)
    if not url:
        return web.json_response({"error": "unknown link"}, status=400)
    subprocess.Popen(["cmd", "/c", "start", "", url])
    return web.json_response({"opened": name})


def _record_terminal(fields, verdict):
    # P2c: id list gets RAN, DENIED, STOPPED, DUPLICATE, and non-signature
    # REFUSED. Content hash only for RAN, DENIED, STOPPED. SPOOFED stays off.
    protocol.set_approvals_log(LOG)
    if verdict.kind == "SPOOFED" or verdict.http == "SPOOFED REPLAY":
        return
    if verdict.kind not in ("RAN", "DENIED", "STOPPED", "DUPLICATE", "REFUSED"):
        return
    try:
        protocol.record_seen(SEEN, fields, verdict)
        _flush_seen_events()
        if _seen_alarm_state != "alarm":
            _note_seen_ok()
    except protocol.SeenListError:
        _note_seen_alarm()
        raise


def _drop_unfinished_claim(fields):
    """P2b: a PANE_MISMATCH after claim must not leave the id on the seen-list."""
    job_id = str((fields or {}).get("id", "") or "")
    if not job_id:
        return
    protocol.set_approvals_log(LOG)
    try:
        protocol.drop_claim(SEEN, job_id)
        _flush_seen_events()
    except protocol.SeenListError:
        pass


def _claim_run(fields):
    protocol.set_approvals_log(LOG)
    protocol.claim_seen(SEEN, fields)
    _flush_seen_events()


def _finalize_run(fields, verdict):
    protocol.set_approvals_log(LOG)
    protocol.finalize_seen(SEEN, fields, verdict)
    _flush_seen_events()
    if _seen_alarm_state != "alarm":
        _note_seen_ok()


def _safe_reply(verdict, fields, extra=None):
    try:
        return protocol.format_reply(verdict, fields, extra)
    except Exception:
        return "VERDICT: REFUSED\n" + protocol.STOP_TOKEN


def build_autoreopen():
    """Always off for this spec. Do not auto-run leftover CLAIMED ids."""
    return False


def policy_allows_without_approval(fields):
    """The Build approvals panel was removed. A ready signed job does not wait for a click."""
    return True


APPROVAL_MAX_AGE = timedelta(hours=24)


def approval_bind(fields):
    src = fields if isinstance(fields, dict) else {}
    sig = str(src.get("sig") or "")
    if sig:
        return hashlib.sha256(sig.encode("ascii")).hexdigest()
    return protocol.content_fingerprint(src)


def _parse_approval_time(text):
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        else:
            parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = protocol._naive_as_et(parsed)
        return parsed
    except ValueError:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            return protocol._naive_as_et(parsed)
        except ValueError:
            return None


def record_approval(job_id, pane="", fields=None):
    """Record an approval bind. Handoff-sourced jobs must route to the watcher."""
    src = fields if isinstance(fields, dict) else {}
    found, _owner, source = find_job(job_id)
    if source == SOURCE_SHADOW:
        _note_shadow_alarm(job_id)
        return False
    if not src and found:
        src = found
    check = src or found or {}
    if source == SOURCE_HANDOFF and not routes_to_watcher(check):
        return False
    record({
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "id": job_id,
        "decision": "approve",
        "pane": pane or src.get("pane", ""),
        "bind": approval_bind(src) if src else "",
        # lane "build" is a log label only. Binding is row.bind / approval_bind, not this field.
        "lane": "build",
    })
    return True


def last_hub_decision(job_id):
    """Last approve/deny in the approvals log. Empty if none.

    First-decision-final applies to watcher/handoff jobs. Hub-pane bus
    jobs are already single-run via the seen list and claim.
    """
    job_id = str(job_id or "")
    if not job_id or not LOG.is_file():
        return ""
    last = ""
    try:
        text = LOG.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("id") != job_id:
            continue
        decision = str(row.get("decision") or "")
        if decision in ("approve", "deny"):
            last = decision
    return last


def is_approved(job_id, fields=None, *, now=None):
    job_id = str(job_id or "")
    if not job_id or not LOG.is_file():
        return False
    clock = now if now is not None else datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    bind = approval_bind(fields) if fields is not None else ""
    try:
        text = LOG.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("id") != job_id or row.get("decision") != "approve":
            continue
        stored = str(row.get("bind") or "")
        if not stored:
            continue
        if bind and not _sigs_match(bind, stored):
            continue
        when = _parse_approval_time(row.get("time", ""))
        if when is None or clock - when > APPROVAL_MAX_AGE:
            continue
        return True
    return False


ALLOWED_PANES = ("", "1", "2")


def signed_to(fields=None):
    """Trim ``to``, then compare it case-sensitively."""
    src = fields if isinstance(fields, dict) else {}
    return str(src.get("to") or "").strip()


def signed_pane(fields=None):
    """Exact pane token. Not trimmed — `` 1`` and ``2 `` are BAD_PANE."""
    src = fields if isinstance(fields, dict) else {}
    raw = src.get("pane", "")
    if raw is None:
        raw = ""
    return str(raw)


def pane_reason(fields=None):
    """pane must be exactly empty, ``1``, or ``2``. ``01``, `` 1``, ``2 ``, unicode digits: BAD_PANE."""
    if signed_pane(fields) not in ALLOWED_PANES:
        return "BAD_PANE"
    return ""


def routes_to_watcher(fields=None):
    """Single routing rule (BUILD-CLAIM rev 3). Both paths call this.

    Watcher when verified ``to`` is exactly ``Build`` after trim
    (case-sensitive), ``lane`` is not ``lab``, and ``pane`` is empty or
    ``2``. ``pane`` ``1`` goes to hub. Any other pane is BAD_PANE, not a
    watcher job. File location does not decide.
    """
    src = fields if isinstance(fields, dict) else {}
    dest = signed_to(src)
    lane = str(src.get("lane") or "").strip().lower()
    pane = signed_pane(src)
    if pane_reason(src):
        return False
    return dest == "Build" and lane != "lab" and pane != "1"


def is_build_lane(fields=None):
    return routes_to_watcher(fields)


def _sigs_match(posted, stored):
    left = str(posted or "")
    right = str(stored or "")
    if not left or not right or len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)


def _peer_host(request):
    peer = None
    transport = getattr(request, "transport", None)
    if transport is not None:
        peer = transport.get_extra_info("peername")
    host = ""
    if peer:
        host = peer[0]
    elif getattr(request, "remote", None):
        host = request.remote
    host = str(host or "").strip().lower()
    if host.startswith("::ffff:"):
        host = host[7:]
    return host


def is_localhost_request(request):
    return _peer_host(request) in ("127.0.0.1", "::1", "localhost")


def claim_reply_mac(job_id, nonce, verdict, token=None):
    raw = TOKEN if token is None else token
    key_text = "" if raw is None else str(raw)
    if not key_text:
        raise ValueError("empty claim mac key")
    key = key_text.encode("utf-8")
    message = "%s\n%s\n%s" % (job_id, nonce, verdict)
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


def claim_host_ok(header):
    """Host must be exactly HUB_HTTP_HOST. Other Host values get HTTP 421."""
    return str(header or "") == HUB_HTTP_HOST


def claim_reply_text(verdict, *, wait=False, store_error=False):
    if store_error:
        return "REFUSED STORE_ERROR"
    if wait:
        return "WAIT NEEDS_APPROVE"
    kind = str(getattr(verdict, "kind", "") or "")
    reason = str(getattr(verdict, "reason", "") or "")
    http = str(getattr(verdict, "http", "") or "")
    if kind in ("OK",) or getattr(verdict, "ready", False):
        return "GO"
    if kind == "DUPLICATE":
        if reason == "SAME_CONTENT":
            return "DUPLICATE SAME_CONTENT"
        return "DUPLICATE"
    if kind == "SPOOFED":
        return "SPOOFED"
    if reason in ("STALE", "BAD_TIME", "MALFORMED_TIME"):
        return "REFUSED " + reason
    if http.startswith("REFUSED "):
        return http
    if http:
        if http in ("STALE", "BAD_TIME", "MALFORMED_TIME", "STORE_ERROR", "BAD_PANE", "UNMAPPED"):
            return "REFUSED " + http
        if http.startswith("SPOOFED"):
            return "SPOOFED"
        return http
    if kind == "REFUSED" and reason:
        return "REFUSED " + reason
    return kind or "REFUSED"


def ids_with_return():
    found = set()
    for name in RETURNS.values():
        path = BUS / name
        if path.is_file():
            found.update(protocol.return_ids_in(read_bus(path)))
    return found


def archived_job_ids():
    found = set()
    for path in BUS.glob("*-ARCHIVE-*.md"):
        try:
            for fields in blocks(read_bus(path)):
                job_id = fields.get("id", "")
                if job_id:
                    found.add(job_id)
        except Exception:
            continue
    return found


def _finished_return(job_id):
    """A RAN/DENIED/STOPPED return. SPOOFED does not consume the id."""
    job_id = str(job_id or "")
    if not job_id:
        return False
    for name in RETURNS.values():
        path = BUS / name
        if not path.is_file():
            continue
        kind = protocol.first_verdict_for_id(read_bus(path), job_id)
        if kind in ("RAN", "DENIED", "STOPPED"):
            return True
    return False


def claimed_not_returned():
    try:
        rows = protocol.read_seen(SEEN, alarm_claims=False)
    except protocol.SeenListError:
        return []
    returned = ids_with_return()
    items = []
    for row in rows:
        if not protocol.is_claim(row):
            continue
        job_id = row.get("id", "")
        if job_id and job_id not in returned:
            items.append({
                "id": job_id,
                "status": "CLAIMED-NOT-RETURNED",
                "pane": row.get("pane", ""),
                "at": row.get("at", ""),
            })
    return items


def pending_watcher_approvals():
    """The approvals panel is gone. Nothing is listed as waiting on a click."""
    return []


def needs_you():
    return claimed_not_returned() + pending_watcher_approvals()


def _note_archive_alarm(job_id="", reason="ARCHIVE"):
    record({
        "time": now(),
        "id": job_id,
        "decision": "alarm",
        "reason": reason,
    })


def archive_job(fields):
    job_id = str((fields or {}).get("id", "") or "")
    if not job_id:
        return
    for name in OWNERS:
        path = BUS / name
        if not path.is_file():
            continue
        try:
            parsed = blocks(read_bus(path))
        except Exception:
            continue
        if any(item.get("id") == job_id for item in parsed):
            try:
                protocol.move_finished_to_archive(path, {job_id})
            except protocol.LockError:
                _note_archive_alarm(job_id, reason="LOCK")
            except protocol.ArchiveError:
                _note_archive_alarm(job_id)
            return


def sync_returns():
    """Finalize leftover CLAIMED ids that now have a return, then archive."""
    returned = ids_with_return()
    if not returned:
        for name in OWNERS:
            path = BUS / name
            if path.is_file():
                try:
                    finished = {item.get("id", "") for item in blocks(read_bus(path))}
                    finished &= archived_job_ids() | ids_with_return()
                    if finished:
                        try:
                            protocol.move_finished_to_archive(path, finished)
                        except (protocol.ArchiveError, protocol.LockError):
                            _note_archive_alarm()
                except Exception:
                    continue
        return
    try:
        rows = protocol.read_seen(SEEN, alarm_claims=False)
    except protocol.SeenListError:
        rows = []
    for row in rows:
        job_id = row.get("id", "")
        if not job_id or job_id not in returned:
            continue
        if not protocol.is_claim(row):
            continue
        fields, owner, _source = find_job(job_id)
        if not fields:
            continue
        kind = protocol.first_verdict_for_id(
            read_bus(BUS / RETURNS[owner]) if (BUS / RETURNS[owner]).is_file() else "",
            job_id,
        ) or "RAN"
        try:
            _finalize_run(fields, protocol.Verdict(kind, "", http=kind))
        except protocol.SeenListError:
            _note_seen_alarm()
    finished = set(returned) | archived_job_ids()
    for name in OWNERS:
        path = BUS / name
        if not path.is_file():
            continue
        try:
            present = {item.get("id", "") for item in blocks(read_bus(path))}
        except Exception:
            continue
        move = {job_id for job_id in present if job_id and job_id in finished}
        if move:
            try:
                protocol.move_finished_to_archive(path, move)
            except (protocol.ArchiveError, protocol.LockError):
                _note_archive_alarm()


def claim_job(job_id, sig, *, remote="127.0.0.1"):
    """Watcher claim. Reuses claim_seen. Never signs, never runs, never writes a return."""
    host = str(remote or "").strip().lower()
    if host.startswith("::ffff:"):
        host = host[7:]
    if host not in ("127.0.0.1", "::1", "localhost"):
        return "REFUSED"
    job_id = str(job_id or "")
    protocol.set_approvals_log(LOG)
    try:
        sync_returns()
    except Exception:
        pass
    fields, owner, source = find_job(job_id)
    if source == SOURCE_SHADOW:
        _note_shadow_alarm(job_id)
        return "REFUSED SHADOW"
    if not fields or not owner:
        return "REFUSED"
    if source == SOURCE_HANDOFF and is_staging() and is_live_handoff_path(handoff_dir()):
        return "REFUSED LIVE_HANDOFF"
    if source == SOURCE_HANDOFF and not routes_to_watcher(fields):
        return "REFUSED WRONG_PATH"
    stored = str(fields.get("sig", "") or "")
    if not _sigs_match(sig, stored):
        return "SPOOFED"
    try:
        verdict = evaluate(fields, owner)
    except protocol.SeenListError:
        _note_seen_alarm()
        return "REFUSED SEEN_LIST"
    except Exception:
        return "REFUSED MALFORMED"
    if verdict.kind == "SPOOFED":
        log_spoofed(fields, verdict.reason or "BAD_SIG", once=True)
        return "SPOOFED"
    if not verdict.ready:
        return claim_reply_text(verdict)
    if not routes_to_watcher(fields):
        return "REFUSED WRONG_PATH"
    if job_id in ids_with_return() or job_id in archived_job_ids():
        return "DUPLICATE"
    if not policy_allows_without_approval(fields) and not is_approved(job_id, fields):
        return "WAIT NEEDS_APPROVE"
    lock_path = Path(SEEN).with_name(".claim.lock")
    with protocol.lock_bus(lock_path):
        try:
            rec = protocol.find_seen(SEEN, job_id, alarm_claims=False)
        except protocol.SeenListError:
            rec = None
        if rec:
            return "DUPLICATE"
        if job_id in ids_with_return() or job_id in archived_job_ids():
            return "DUPLICATE"
        try:
            _claim_run(fields)
        except protocol.SeenListError:
            _note_seen_alarm()
            return "REFUSED STORE_ERROR"
        return "GO"


def watcher_try_claim(job_id, sig, *, call=None):
    """Fail closed: hub down or anything but GO means no action and no return."""
    try:
        verdict = (call or claim_job)(job_id, sig)
    except Exception:
        return "REFUSED HUB_DOWN"
    if verdict is None:
        return "REFUSED HUB_DOWN"
    return str(verdict)


def claim_helper_exit(verdict, *, error=False):
    """Exit codes for tools/Claim-BusJob.ps1: 0 GO, 2 NO-GO, 3 ERROR."""
    if error:
        return CLAIM_EXIT_ERROR
    text = str(verdict or "").strip()
    if text == "GO":
        return CLAIM_EXIT_GO
    if text:
        return CLAIM_EXIT_NOGO
    return CLAIM_EXIT_ERROR


def claim_helper_trust(status_code, mac_valid, verdict=""):
    """Helper: anything but HTTP 200 and a valid HMAC is exit 3."""
    if int(status_code or 0) != 200 or not mac_valid:
        return CLAIM_EXIT_ERROR
    return claim_helper_exit(verdict)


def watcher_may_act(verdict, exit_code=None):
    if exit_code is not None:
        return int(exit_code) == CLAIM_EXIT_GO
    return str(verdict or "").strip() == "GO"


def resume_watcher(act=None):
    """Read returns and archive first. Skip returned ids. Never auto-run leftover CLAIMED."""
    skipped = ids_with_return() | archived_job_ids()
    leftover = {item["id"] for item in claimed_not_returned()}
    pending = []
    for name, owner in OWNERS.items():
        path = BUS / name
        if not path.is_file():
            continue
        try:
            parsed = blocks(read_bus(path))
        except Exception:
            continue
        for fields in parsed:
            if fields.get("id"):
                pending.append((fields, owner))
    results = []
    for fields, owner in pending:
        job_id = fields.get("id", "")
        if job_id in skipped:
            results.append((job_id, "SKIP_RETURN"))
            continue
        if job_id in leftover:
            results.append((job_id, "CLAIMED-NOT-RETURNED"))
            continue
        if not routes_to_watcher(fields):
            results.append((job_id, "SKIP_HUB"))
            continue
        verdict = claim_job(job_id, fields.get("sig", ""))
        results.append((job_id, verdict))
        if act is not None and watcher_may_act(verdict):
            act(fields, owner)
    try:
        sync_returns()
    except Exception:
        pass
    return results


def apply_decision(job_id, decision, *, lane="", view_hash=""):
    try:
        return _apply_decision(job_id, decision, lane=lane, view_hash=view_hash)
    except (OSError, protocol.LockError):
        return 403, {"error": "RETURN"}


def _apply_decision(job_id, decision, *, lane="", view_hash=""):
    del lane  # lane comes from the signed job, never the request
    protocol.set_approvals_log(LOG)
    fields, owner, source = find_job(job_id)
    if source == SOURCE_SHADOW:
        _note_shadow_alarm(job_id)
        return 403, {"error": "SHADOW"}
    if not fields or decision not in ("approve", "deny"):
        return 400, {"error": "unknown job or decision"}
    # Watcher/handoff only. Hub-pane bus jobs are already single-run via seen+claim.
    if routes_to_watcher(fields) or source == SOURCE_HANDOFF:
        prior = last_hub_decision(job_id)
        if prior:
            if prior == decision:
                return 403, {"error": "DUPLICATE"}
            return 403, {"error": "ALREADY_DECIDED"}
    if view_hash:
        expected = protocol.content_fingerprint(fields)
        got = str(view_hash)
        if len(got) != len(expected) or not hmac.compare_digest(got, expected):
            return 409, {"error": "STALE"}
    try:
        verdict = evaluate(fields, owner)
    except protocol.SeenListError:
        _note_seen_alarm()
        verdict = protocol.Verdict("REFUSED", "SEEN_LIST", http="SEEN_LIST")
    except Exception:
        verdict = protocol.Verdict("REFUSED", "MALFORMED", http="MALFORMED")
    if not verdict.ready:
        if verdict.kind == "SPOOFED" or verdict.http == "SPOOFED REPLAY":
            log_spoofed(fields, verdict.reason or "REPLAY")
        elif verdict.http in ("BAD_PANE", "UNMAPPED", "BAD_ID"):
            record({"time": now(), "id": job_id, "decision": "held", "reason": verdict.http})
        else:
            record({
                "time": now(),
                "id": job_id,
                "decision": verdict.kind,
                "reason": verdict.reason or verdict.kind,
            })
        try:
            _record_terminal(fields, verdict)
        except protocol.SeenListError:
            append_return(owner, _safe_reply(verdict, fields))
            return 403, {"error": "SEEN_LIST"}
        append_return(owner, _safe_reply(verdict, fields))
        return 403, {"error": verdict.http or verdict.kind}
    if source == SOURCE_HANDOFF and not routes_to_watcher(fields):
        return 403, {"error": "WRONG_PATH"}
    if source == SOURCE_ARCHIVE or _finished_return(job_id):
        return 403, {"error": "DUPLICATE"}
    pane = resolve_pane(fields)
    if decision == "deny":
        denied = protocol.Verdict("DENIED", "", http="denied")
        record({"time": now(), "id": job_id, "decision": "deny", "pane": pane})
        try:
            _record_terminal(fields, denied)
        except protocol.SeenListError:
            append_return(owner, _safe_reply(denied, fields, extra=["result: denied"]))
            return 403, {"error": "SEEN_LIST"}
        append_return(owner, _safe_reply(denied, fields, extra=["result: denied"]))
        return 200, {"result": "denied"}
    if routes_to_watcher(fields) or source == SOURCE_HANDOFF:
        if is_approved(job_id, fields):
            return 403, {"error": "DUPLICATE"}
        if not record_approval(job_id, pane=pane, fields=fields):
            return 403, {"error": "WRONG_PATH"}
        return 200, {"result": "approved"}
    record({"time": now(), "id": job_id, "decision": "approve", "pane": pane})
    try:
        code, out, err = run_pane(pane, fields["job"], fields, owner)
    except Exception:
        refused = protocol.Verdict("REFUSED", "MALFORMED", http="MALFORMED")
        append_return(owner, _safe_reply(refused, fields))
        return 403, {"error": "MALFORMED"}
    if code == 403 and err in ("SEEN_LIST", "STORE_ERROR", "WRONG_PATH"):
        refused = protocol.Verdict("REFUSED", err, http=err)
        append_return(owner, _safe_reply(refused, fields))
        return 403, {"error": err}
    if code == 403 and err:
        spoofed = protocol.Verdict("SPOOFED", err, http=f"SPOOFED {err}")
        log_spoofed(fields, err)
        append_return(owner, _safe_reply(spoofed, fields))
        return 403, {"error": f"SPOOFED {err}"}
    result = scrub_tail(out.strip() or err.strip() or f"exit {code}")
    ran = protocol.Verdict("RAN", "", http="RAN")
    try:
        remember(pane, job_id, result)
    except OSError:
        pass
    extra = [f"pane: {pane}", f"exit: {code}", f"result: {result}"]
    try:
        _finalize_run(fields, ran)
    except protocol.SeenListError:
        _note_seen_alarm()
        failed = list(extra) + ["RECORD: FAILED"]
        append_return(owner, _safe_reply(ran, fields, extra=failed))
        return 403, {"error": "RAN RECORD_FAILED"}
    reply = _safe_reply(ran, fields, extra=extra)
    try:
        append_return(owner, reply)
    except (OSError, protocol.LockError):
        _note_return_alarm(job_id)
        return 200, {
            "result": result,
            "exit": code,
            "return": "ALARM",
            "reply": reply,
        }
    return 200, {"result": result, "exit": code}


def job_preview_payload(job_id):
    """Side-effect-free preview: id, from, lane, summary, hash. No raw sig."""
    global _preview_quiet
    prev = _preview_quiet
    _preview_quiet = True
    try:
        job_id = str(job_id or "")
        fields, _owner, source = find_job(job_id)
        if source == SOURCE_SHADOW:
            return 403, {"error": "SHADOW"}
        if not fields:
            return 404, {"error": "unknown job"}
        if source == SOURCE_HANDOFF and not routes_to_watcher(fields):
            return 403, {"error": "WRONG_PATH"}
        job = str(fields.get("job") or "")
        return 200, {
            "id": str(fields.get("id") or job_id),
            "from": str(fields.get("from") or ""),
            "lane": str(fields.get("lane") or ""),
            "summary": job[:240],
            "hash": protocol.content_fingerprint(fields),
            "bind": approval_bind(fields),
        }
    finally:
        _preview_quiet = prev


async def job_preview(request):
    if not allowed(request):
        return web.json_response({"error": "refused"}, status=403)
    job_id = request.match_info.get("job_id") or request.query.get("id") or ""
    status, payload = job_preview_payload(job_id)
    return web.json_response(payload, status=status)


async def decide(request):
    if not allowed(request):
        return web.json_response({"error": "refused"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "bad json"}, status=400)
    view_hash = body.get("hash")
    if not isinstance(view_hash, str) or not view_hash:
        return web.json_response({"error": "HASH_REQUIRED"}, status=400)
    try:
        status, payload = apply_decision(
            body.get("id", ""),
            body.get("decision", ""),
            view_hash=view_hash,
        )
    except Exception:
        return web.json_response({"error": "RETURN"}, status=403)
    if not isinstance(status, int) or status >= 500:
        return web.json_response({"error": "RETURN"}, status=403)
    return web.json_response(payload, status=status)


async def claim(request):
    if not is_localhost_request(request):
        return web.json_response({"verdict": "REFUSED"}, status=403)
    if not claim_host_ok(request.headers.get("Host", "")):
        return web.json_response({"verdict": "REFUSED"}, status=403)
    if not allowed(request):
        return web.json_response({"verdict": "REFUSED TOKEN"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"verdict": "REFUSED"}, status=400)
    job_id = body.get("id", "") if isinstance(body, dict) else ""
    sig = body.get("sig", "") if isinstance(body, dict) else ""
    nonce = body.get("nonce", "") if isinstance(body, dict) else ""
    verdict = claim_job(job_id, sig, remote=_peer_host(request))
    mac = claim_reply_mac(job_id, nonce, verdict)
    return web.json_response({"verdict": verdict, "mac": mac}, status=200)


def main():
    async def _run():
        runner = await start_hub()
        stop = asyncio.Event()
        try:
            await stop.wait()
        finally:
            await runner.cleanup()
            clear_hub_process()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        return


def _retired():
    return


class Handler_retired:
    def log_message(self, fmt, *args):
        return

    def send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 100_000:
            return None
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        if self.path == "/api/state":
            self.send_json(200, {
                "class": "pattern-lab",
                "live": False,
                "freebots": "off",
                "hivebrain_game_chat": "off",
                "queue": queue(),
                "sessions": sessions(),
                "log": LOG.read_text(encoding="utf-8") if LOG.is_file() else "",
                "bus": bus_view(),
                "panes": ["1", "2"],
            })
            return
        rel = "index.html" if self.path in ("/", "") else self.path.lstrip("/")
        if ".." in rel or "\\" in rel or rel.startswith("bus") or rel.startswith("panes"):
            self.send_error(404)
            return
        path = (ROOT / rel).resolve()
        if not str(path).startswith(str(ROOT)) or not path.is_file():
            self.send_error(404)
            return
        kind = {".html": "text/html", ".css": "text/css", ".js": "text/plain"}.get(path.suffix, "text/plain")
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path == "/api/open":
            self.open_link()
            return
        if self.path == "/api/signin":
            self.sign_in()
            return
        if self.path != "/api/decide":
            self.send_error(404)
            return
        self.decide()

    def open_link(self):
        body = self.read_json() or {}
        name = body.get("name", "")
        if name == "grokbot":
            link = Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Grok Bot.lnk"
            if not link.is_file():
                self.send_json(404, {"error": "Grok Bot shortcut not found"})
                return
            subprocess.Popen(["cmd", "/c", "start", "", str(link)])
            self.send_json(200, {"opened": "grokbot"})
            return
        url = LINKS.get(name)
        if not url:
            self.send_json(400, {"error": "unknown link"})
            return
        subprocess.Popen(["cmd", "/c", "start", "", url])
        self.send_json(200, {"opened": name})

    def sign_in(self):
        pane = (self.read_json() or {}).get("pane", "")
        if pane not in ("1", "2"):
            self.send_json(400, {"error": "pane must be 1 or 2"})
            return
        home = ROOT / "panes" / f"pane-{pane}"
        home.mkdir(parents=True, exist_ok=True)
        if not GROK.is_file():
            self.send_json(404, {"error": "grok.exe not found"})
            return
        subprocess.Popen(
            ["cmd", "/c", "start", "HiveBrainComms sign-in", "cmd", "/k",
             f"set GROK_HOME={home}&& \"{GROK}\" login"],
        )
        record({"time": now(), "id": f"signin-{pane}", "decision": "signin-opened", "pane": pane})
        self.send_json(200, {"opened": pane})

    def decide(self):
        body = self.read_json() or {}
        view_hash = body.get("hash")
        if not isinstance(view_hash, str) or not view_hash:
            self.send_json(400, {"error": "HASH_REQUIRED"})
            return
        status, payload = apply_decision(
            body.get("id", ""),
            body.get("decision", ""),
            view_hash=view_hash,
        )
        self.send_json(status, payload)


if __name__ == "__main__":
    main()
