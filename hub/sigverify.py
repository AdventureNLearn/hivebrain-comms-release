"""HMAC-SHA256 bus-job verifier. Fail closed. No off switch."""

import ctypes
import hashlib
import hmac
import os
import re
import sys
from pathlib import Path

from hub import lanes as _lanes

FORMAT_VERSION = "2"
SIGNED_FIELDS = (
    "v",
    "id",
    "from",
    "to",
    "lane",
    "priority",
    "time",
    "routed",
    "pane",
    "job",
    "done-when",
)

# Pane when the signed pane field is empty. Keys are (to, lane).
PANE_MAP = {
    ("Build", "lab"): "1",
    ("Build", "ops"): "2",
}

_LANES = _lanes.load()
KEY_FILES = dict(_LANES.key_files)
OWNER_ALIASES = dict(_LANES.aliases)

SIG_HEX = re.compile(r"^[0-9a-f]{64}$")
SPACE = re.compile(r"\s+")
FIELD_LINE = re.compile(r"^([a-z][a-z0-9-]*): ?(.*)$")

# Names that look like a request to turn verification off. They never do.
_DISABLE_ENV = (
    "HBC_SKIP_SIG",
    "HBC_SKIP_SIGVERIFY",
    "HBC_DISABLE_SIG",
    "HBC_DISABLE_SIGVERIFY",
    "HBC_NO_SIGVERIFY",
    "HBC_SIG_OFF",
    "HBC_SIGVERIFY",
    "SKIP_SIGVERIFY",
    "DISABLE_SIGVERIFY",
)
_DISABLE_ARGS = {
    "--skip-sig",
    "--skip-sigverify",
    "--no-sigverify",
    "--disable-sigverify",
    "--sig-off",
}
_DISABLE_OFF = {"0", "false", "off", "no", "disable", "disabled"}

_key_provider = None
_keys_dir = None


class _Fail(Exception):
    def __init__(self, code):
        self.code = code


def set_key_provider(fn):
    """Test seam: fn(owner) -> key bytes, or None if missing. Not a disable switch."""
    global _key_provider
    _key_provider = fn


def clear_key_provider():
    global _key_provider
    _key_provider = None


def set_keys_dir(path):
    """Test seam for a temporary keys folder. Default is the user's .grok keys folder."""
    global _keys_dir
    _keys_dir = Path(path) if path is not None else None


def ignore_disable_settings(argv=None, environ=None):
    """Ignore any setting that asks to turn verification off. Log and keep it on."""
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    for name in _DISABLE_ENV:
        raw = environ.get(name)
        if raw is None or raw == "":
            continue
        if name == "HBC_SIGVERIFY" and raw.strip().lower() not in _DISABLE_OFF:
            continue
        print("ignored disable setting", name, flush=True)
    for arg in argv:
        if arg.lower() in _DISABLE_ARGS:
            print("ignored disable argument", arg, flush=True)


def normalize_owner(name):
    if name is None:
        return None
    return OWNER_ALIASES.get(str(name).strip())


def parse_block_lines(lines):
    """Parse one job block. A lowercase `key:` line starts a field; other lines continue it."""
    fields = {}
    current = None
    for line in lines:
        match = FIELD_LINE.match(line)
        if match:
            current = match.group(1)
            fields[current] = match.group(2)
        elif current is not None:
            fields[current] = (fields[current] + " " + line).strip()
    return fields


def resolve_pane(fields):
    """Signed pane, or the fixed (to, lane) map when pane is empty."""
    src = fields if isinstance(fields, dict) else {}
    raw = src.get("pane", "")
    if raw is None:
        raw = ""
    raw = SPACE.sub(" ", str(raw).strip())
    if raw in ("1", "2"):
        return raw
    if raw != "":
        return ""
    route = (
        SPACE.sub(" ", str(src.get("to", "") or "").strip()),
        SPACE.sub(" ", str(src.get("lane", "") or "").strip()),
    )
    return PANE_MAP.get(route, "")


def canonicalize(fields):
    lines = []
    src = fields if isinstance(fields, dict) else {}
    for name in SIGNED_FIELDS:
        raw = src.get(name, "")
        if raw is None:
            raw = ""
        value = SPACE.sub(" ", str(raw).strip())
        lines.append(f"{name}={value}")
    return "\n".join(lines)


def mac_hex(fields, key):
    message = canonicalize(fields).encode("utf-8")
    return hmac.new(bytes(key), message, hashlib.sha256).hexdigest()


def _wipe(buf):
    if not isinstance(buf, bytearray):
        return
    try:
        if buf:
            view = (ctypes.c_char * len(buf)).from_buffer(buf)
            ctypes.memset(view, 0, len(buf))
    except (TypeError, ValueError, BufferError):
        for i in range(len(buf)):
            buf[i] = 0


def _keys_folder():
    if _keys_dir is not None:
        return _keys_dir
    return Path.home() / ".grok" / "keys"


def _unprotect_current_user(blob):
    if os.name != "nt":
        raise _Fail("DPAPI")
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt32.CryptUnprotectData
    fn.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    fn.restype = wintypes.BOOL

    if not blob:
        raise _Fail("DPAPI")
    in_buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    blob_in = DATA_BLOB(len(blob), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_ubyte)))
    blob_out = DATA_BLOB()
    cryptprotect_ui_forbidden = 0x1
    ok = fn(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        cryptprotect_ui_forbidden,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise _Fail("DPAPI")
    try:
        count = int(blob_out.cbData)
        ptr = blob_out.pbData
        if not ptr or count <= 0:
            raise _Fail("DPAPI")
        out = bytearray(count)
        ctypes.memmove((ctypes.c_ubyte * count).from_buffer(out), ptr, count)
        ctypes.memset(ptr, 0, count)
        return out
    finally:
        if blob_out.pbData:
            kernel32.LocalFree(blob_out.pbData)


def _load_key(owner):
    if _key_provider is not None:
        try:
            material = _key_provider(owner)
        except _Fail:
            raise
        except Exception:
            raise _Fail("NO_KEY")
        if material is None:
            raise _Fail("NO_KEY")
        return bytearray(material)

    name = KEY_FILES.get(owner)
    if not name:
        raise _Fail("OWNER_MISMATCH")
    path = _keys_folder() / name
    try:
        if not path.is_file():
            raise _Fail("NO_KEY")
        blob = path.read_bytes()
    except _Fail:
        raise
    except Exception:
        raise _Fail("NO_KEY")
    try:
        return _unprotect_current_user(blob)
    except _Fail:
        raise
    except Exception:
        raise _Fail("DPAPI")


def verify_job(fields, expected_owner, *, seen=None):
    """Return a short reason code, or '' if the job may run."""
    secret = None
    try:
        if not isinstance(fields, dict):
            raise _Fail("PARSE")
        given = fields.get("sig", "")
        if given is None:
            given = ""
        given = str(given)
        if given.strip() == "":
            raise _Fail("NO_SIG")
        if SIG_HEX.fullmatch(given) is None:
            raise _Fail("PARSE")
        if str(fields.get("v", "") or "").strip() != FORMAT_VERSION:
            raise _Fail("OLD_FORMAT")

        owner = normalize_owner(expected_owner)
        if owner is None:
            raise _Fail("OWNER_MISMATCH")

        secret = _load_key(owner)
        expected = mac_hex(fields, secret)
        if not hmac.compare_digest(expected, given):
            raise _Fail("BAD_SIG")

        claimed = normalize_owner(fields.get("from", ""))
        if claimed != owner:
            raise _Fail("OWNER_MISMATCH")

        job_id = str(fields.get("id", "") or "")
        seen_ids = set() if seen is None else set(seen)
        if job_id and job_id in seen_ids:
            raise _Fail("REPLAY")
        return ""
    except _Fail as exc:
        return exc.code
    except Exception:
        return "PARSE"
    finally:
        _wipe(secret)
