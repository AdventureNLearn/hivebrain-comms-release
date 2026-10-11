"""Bus lane names and owners. Neutral defaults. Local file is gitignored."""

import json
import os
import re
from pathlib import Path

CONFIG_NAME = "lanes.local.json"
ENV_FILE = "HBC_LANES_FILE"

_SAFE_MD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.md$")
_SAFE_BIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.bin$")
_SAFE_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/-]{0,62}$")

DEFAULT_DATA = {
    "status": "BUILDER-STATUS.md",
    "lanes": [
        {
            "id": "Hub",
            "owner": "Hub",
            "inbox": "HUB-TO-BUILD.md",
            "outbox": "BUILD-TO-HUB.md",
            "key": "bus-hub.bin",
        },
        {
            "id": "Builder",
            "owner": "Builder",
            "inbox": "BUILDER-TO-BUILD.md",
            "outbox": "BUILD-TO-BUILDER.md",
            "key": "bus-builder.bin",
        },
    ],
}


class Spec:
    def __init__(self, status, lanes):
        self.status = status
        self.lanes = list(lanes)

    @property
    def owners(self):
        return {lane["inbox"]: lane["owner"] for lane in self.lanes}

    @property
    def returns(self):
        return {lane["owner"]: lane["outbox"] for lane in self.lanes}

    @property
    def key_files(self):
        return {lane["owner"]: lane["key"] for lane in self.lanes}

    @property
    def aliases(self):
        aliases = {}
        for lane in self.lanes:
            owner = lane["owner"]
            aliases[owner] = owner
            token = str(lane.get("id") or "").strip()
            if token:
                aliases[token] = owner
        return aliases

    @property
    def routers(self):
        return tuple(lane["owner"] for lane in self.lanes)

    @property
    def inboxes(self):
        return tuple(lane["inbox"] for lane in self.lanes)


def _clean_name(value, pattern):
    text = str(value or "").strip()
    if pattern.fullmatch(text) is None:
        return ""
    if "/" in text or "\\" in text or ".." in text:
        return ""
    return text


def parse(data):
    """Return a Spec, or None when the payload is unusable."""
    if not isinstance(data, dict):
        return None
    lanes_raw = data.get("lanes")
    if not isinstance(lanes_raw, list) or not lanes_raw:
        return None
    lanes = []
    seen_inbox = set()
    seen_owner = set()
    for item in lanes_raw:
        if not isinstance(item, dict):
            return None
        owner = str(item.get("owner") or "").strip()
        if _SAFE_OWNER.fullmatch(owner) is None:
            return None
        inbox = _clean_name(item.get("inbox"), _SAFE_MD)
        outbox = _clean_name(item.get("outbox"), _SAFE_MD)
        key = _clean_name(item.get("key"), _SAFE_BIN)
        token = str(item.get("id") or owner).strip()
        if not inbox or not outbox or not key or not token:
            return None
        if inbox in seen_inbox or owner in seen_owner:
            return None
        seen_inbox.add(inbox)
        seen_owner.add(owner)
        lanes.append({
            "id": token,
            "owner": owner,
            "inbox": inbox,
            "outbox": outbox,
            "key": key,
        })
    status = _clean_name(data.get("status"), _SAFE_MD)
    if not status:
        owner = lanes[-1]["owner"].split()[0].upper()
        status = f"{owner}-STATUS.md"
        if _SAFE_MD.fullmatch(status) is None:
            status = "BUILDER-STATUS.md"
    return Spec(status, lanes)


def default_spec():
    spec = parse(DEFAULT_DATA)
    if spec is None:
        raise RuntimeError("default bus lanes are invalid")
    return spec


def load(path=None, environ=None, root=None):
    """Read lanes.local.json or HBC_LANES_FILE. Invalid or missing means the defaults."""
    env = os.environ if environ is None else environ
    raw = path
    if raw is None:
        raw = str(env.get(ENV_FILE) or "").strip() or None
    if raw is None and root is not None:
        raw = Path(root) / CONFIG_NAME
    if raw is None:
        here = Path(__file__).resolve().parent.parent / CONFIG_NAME
        raw = here
    candidate = Path(raw)
    if not candidate.is_file():
        return default_spec()
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        return default_spec()
    spec = parse(data)
    return spec if spec is not None else default_spec()


def watcher_prompt(spec):
    names = " and ".join(spec.inboxes)
    return (
        "Resume the handoff-bus watch. Poll the handoff folder every 5 seconds "
        "under BUS-PROTOCOL v2 (rules 14-18; rule 3 and rule 11 amended). "
        f"Watch {names}. "
        "Hard rule: no GO, no action. Do not make the HTTP call yourself. "
        "Claim each job by running powershell -NoProfile -ExecutionPolicy Bypass "
        "-File tools/Claim-BusJob.ps1 -Id <id> -File <bus-file>. "
        "That helper POSTs id, sig, and a per-claim nonce to localhost /api/claim. "
        "Exit 0 is GO. Exit 2 is NO-GO: do nothing. "
        "Any other exit is an error: fail closed, do nothing, write no return. "
        "If the hub is down, unreachable, or answers anything but GO, "
        "do nothing and write no return for that job. "
        "On resume, read the return files and the archive index first. "
        "Skip any id that already has a return, then claim each remaining id. "
        "Do not re-run a finished job. "
        "Never run a leftover CLAIMED id; redo uses -r2. "
        "Ignore hub-routed jobs in those files: claim only when signed to is exactly "
        "Build, lane is not lab, and pane is not 1 (empty pane counts as watcher). "
        "Reply only in the sender return file. Line 1 of every reply is a VERDICT line."
    )


def apply(spec, *, server=None, jobs=None, sigverify=None, protocol=None):
    """Rebind in-memory maps. Used by tests and after load()."""
    if spec is None:
        spec = default_spec()
    owners = dict(spec.owners)
    returns = dict(spec.returns)
    keys = dict(spec.key_files)
    aliases = dict(spec.aliases)
    if server is not None:
        server.OWNERS = owners
        server.RETURNS = returns
        server.WATCHER_PROMPT = watcher_prompt(spec)
        if hasattr(server, "BUS_WATCH_PROMPT"):
            server.BUS_WATCH_PROMPT = server.WATCHER_PROMPT
    if jobs is not None:
        jobs.OWNERS = owners
        jobs.RETURNS = returns
        jobs.STATUS_FILE = spec.status
    if sigverify is not None:
        sigverify.KEY_FILES = keys
        sigverify.OWNER_ALIASES = aliases
    if protocol is not None:
        protocol.ALLOWED_ROUTERS = spec.routers
    return spec
