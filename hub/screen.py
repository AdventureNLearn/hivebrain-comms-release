"""R10 screen safety: plain text only. No secrets or expanded home paths."""

import os
import re
from pathlib import Path

_SIG_LINE = re.compile(r"(?im)^sig:.*$")
_TOKENISH = re.compile(r"(?i)\b(sk-[A-Za-z0-9]{8,}|bearer\s+[A-Za-z0-9._\-]{8,})\b")
_POSIX_HOME = re.compile(r"/home/[^\s/]+")
_USERS_DIR = re.compile(r"(?i)(?:[A-Za-z]:)?[\\/]+Users[\\/]+[^\s\\/]+")


def _homes():
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


def relative_to_handoff(path, handoff):
    """Display path relative to the handoff folder. Never an expanded home path."""
    if path is None:
        return ""
    text = str(path)
    try:
        rel = Path(path).resolve().relative_to(Path(handoff).resolve())
        return str(rel).replace("\\", "/")
    except Exception:
        name = Path(text).name
        return name or "handoff"


def plain_text(value):
    """Job text for a TextBox. Identical characters; never treated as HTML."""
    if value is None:
        return ""
    return str(value)


def screen_safe(text, handoff=None):
    """Plain text for the screen. Redact sig lines, token-like strings, and home paths."""
    out = plain_text(text)
    if not out:
        return ""
    out = _SIG_LINE.sub("sig: <redacted>", out)
    out = _TOKENISH.sub("<redacted>", out)
    if handoff:
        handoff_text = str(Path(handoff))
        if handoff_text and handoff_text in out:
            out = out.replace(handoff_text, "")
            out = out.replace(handoff_text.replace("\\", "/"), "")
    for home in _homes():
        if home and home in out:
            out = out.replace(home, "<home>")
        slash = home.replace("\\", "/")
        if slash and slash in out:
            out = out.replace(slash, "<home>")
    out = _USERS_DIR.sub("<home>", out)
    out = _POSIX_HOME.sub("<home>", out)
    return out
