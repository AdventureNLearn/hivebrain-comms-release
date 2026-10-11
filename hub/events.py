"""R4 event-log hook. This PR only reads. Task 0 / PR #6 appends lines.

File: <hub state folder>/.hub-events.jsonl
PR #6 writer (cursor/build-session-reopen-7f69, head 0d972f7):

    log_hub_event(event, **fields)
    row = {"time": now(), "event": event}  # now() is %Y-%m-%d %H:%M:%S
    # hub restart:  {"time": "...", "event": "hub restart"}
    # build exit:   {"time": "...", "event": "build exit", "pane": "<pane>", "exit": <code or null>}
"""

import json
from datetime import datetime
from pathlib import Path

from hub.tailread import TAIL_BYTES, read_tail

EVENT_LOG_NAME = ".hub-events.jsonl"
EVENTS = ("hub restart", "build exit")
PANES = frozenset(
    {"build"}
    | {f"build{i}" for i in range(2, 9)}
    | {str(i) for i in range(1, 9)}
    | {f"pane{i}" for i in range(1, 9)}
)
EXIT_MIN = -1_048_576
EXIT_MAX = 1_048_576


def event_log_path(root):
    return Path(root) / EVENT_LOG_NAME


def allowed_pane(value):
    return str(value) in PANES


def coerce_exit(value):
    """Bounded int or None. bool is not an exit code."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, int):
            number = value
        else:
            number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if number < EXIT_MIN or number > EXIT_MAX:
        return None
    return number


def append_event(path, event, pane=None, exit_code=None, when=None):
    """Writer seam matching PR #6 log_hub_event. Not called from server.py in this PR."""
    if event not in EVENTS:
        raise ValueError("unknown event")
    if when is None:
        when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = {"time": str(when), "event": event}
    if pane is not None and allowed_pane(pane):
        row["pane"] = str(pane)
    if event == "build exit" or exit_code is not None:
        row["exit"] = coerce_exit(exit_code)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    return row


def _parse_row(row):
    if not isinstance(row, dict):
        return None
    event = row.get("event")
    if event not in EVENTS:
        return None
    item = {
        "time": str(row.get("time") or ""),
        "event": event,
    }
    pane = row.get("pane")
    if pane is not None and allowed_pane(pane):
        item["pane"] = str(pane)
    if "exit" in row:
        exit_code = coerce_exit(row.get("exit"))
        if exit_code is not None:
            item["exit"] = exit_code
    return item


def read_last_events(path, limit=10):
    """Last `limit` valid #6-shaped events. Never raises. Tail-capped."""
    try:
        if limit <= 0:
            return []
        text = read_tail(path, max_bytes=TAIL_BYTES)
        if not text:
            return []
        found = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, ValueError, UnicodeDecodeError, TypeError):
                continue
            item = _parse_row(row)
            if item is not None:
                found.append(item)
        return found[-limit:]
    except (OSError, ValueError, UnicodeDecodeError, TypeError):
        return []
    except Exception:
        return []
