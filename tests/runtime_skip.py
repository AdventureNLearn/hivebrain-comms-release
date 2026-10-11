"""Skip helpers for Windows Application Control, missing dotnet, and WinError 1314."""

import os
import shutil
from pathlib import Path


DOTNET_BLOCKED = (
    "application control",
    "wdac",
    "smart app control",
    "unauthorizedaccess",
    "blocked by policy",
)


def log_skip(test, reason):
    print("skip: %s" % reason, flush=True)
    test.skipTest(reason)


def dotnet_path():
    home = Path.home() / ".dotnet" / "dotnet"
    if home.is_file():
        return str(home)
    found = shutil.which("dotnet")
    return found


def skip_if_no_dotnet(test):
    if not dotnet_path():
        log_skip(test, "dotnet is missing")


def skip_if_dotnet_unrunnable(test, code, output, *, label):
    text = output or ""
    lowered = text.lower()
    if code == 0:
        return
    if any(needle in lowered for needle in DOTNET_BLOCKED):
        log_skip(
            test,
            "%s: built exe cannot run (Application Control): %s"
            % (label, text.strip()[:240]),
        )


def skip_symlink_denied(test, exc):
    winerr = getattr(exc, "winerror", None)
    if winerr == 1314:
        log_skip(test, "Windows denied symlink/junction (WinError 1314).")
    raise exc


def _isjunction_fallback(path):
    if os.name != "nt":
        return False
    try:
        attrs = os.lstat(str(path)).st_file_attributes
        return bool(attrs & 0x400) and not os.path.islink(str(path))
    except (AttributeError, OSError):
        return False


if not hasattr(os.path, "isjunction"):
    os.path.isjunction = _isjunction_fallback


def is_reparse_point(path):
    text = str(path)
    if os.path.islink(text):
        return True
    isj = getattr(os.path, "isjunction", None)
    if isj and isj(text):
        return True
    if os.name == "nt":
        try:
            attrs = os.lstat(text).st_file_attributes
            return bool(attrs & 0x400)
        except (AttributeError, OSError):
            return False
    return False
