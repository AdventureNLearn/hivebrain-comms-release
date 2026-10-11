"""Single handoff-folder setting. Default is the project lab bus/.

The app and the hub both read control.local.json (gitignored). HBC_HANDOFF
is a hub-side fallback only, used when that file is missing or has no
handoff key; it is not a second app setting. The tab never writes the
configured handoff folder.
"""

import json
import os
import re
from pathlib import Path

CONFIG_NAME = "control.local.json"
ENV_HANDOFF = "HBC_HANDOFF"
DEFAULT_HANDOFF = "bus"
WRITE_KEYS = frozenset({
    "write",
    "writable",
    "write_handoff",
    "approvals",
    "approvals_path",
    "log",
    "log_path",
    "allow_write",
})
_USERS_DIR = re.compile(r"(?i)(?:[A-Za-z]:)?[\\/]+Users[\\/]+[^\s\\/]+")
_POSIX_HOME = re.compile(r"/home/[^\s/]+")
_ETC_ABS = re.compile(r"(?i)^(?:[A-Za-z]:)?[/\\]+etc(?:[/\\]|$)")
_ETC_REL = re.compile(r"(?i)(?:^|/)\.\./etc(?:/|$)")
REJECTED_DISPLAY = "rejected"


def default_handoff(root):
    return Path(root).resolve() / DEFAULT_HANDOFF


def config_path(root):
    return Path(root) / CONFIG_NAME


def _under_users_root(path):
    text = str(path).replace("\\", "/")
    return bool(_USERS_DIR.search(text) or _POSIX_HOME.search(text))


def _is_etc(path):
    """System /etc is shown as outside. Never leak /etc/... as a relative."""
    text = str(path).replace("\\", "/")
    if _ETC_ABS.match(text) or _ETC_REL.search(text):
        return True
    try:
        text = str(Path(path).resolve()).replace("\\", "/")
    except Exception:
        return False
    return bool(_ETC_ABS.match(text))


def _abspath(path):
    return os.path.abspath(os.path.expanduser(str(path)))


def _under_dir(child, parent):
    """True when child is parent or inside it. Uses os.path so Windows case matches."""
    try:
        child_abs = _abspath(child)
        parent_abs = _abspath(parent)
        return os.path.commonpath([child_abs, parent_abs]) == parent_abs
    except (ValueError, OSError, TypeError):
        return False


def display_handoff(path, root=None, home=None):
    """Short relative or ~ form. Never another user's folder name."""
    try:
        resolved = Path(_abspath(path))
    except Exception:
        return Path(path).name or DEFAULT_HANDOFF
    if _is_etc(resolved) or _is_etc(path):
        return "outside"
    if home is None:
        try:
            home = Path.home()
        except Exception:
            home = None
    own_home = bool(home) and _under_dir(resolved, home)
    if _under_users_root(resolved) and not own_home:
        return "outside"
    if root and _under_dir(resolved, root):
        try:
            rel = os.path.relpath(str(resolved), _abspath(root)).replace("\\", "/")
            return _safe_display(rel or DEFAULT_HANDOFF, resolved, home)
        except Exception:
            pass
    if home and own_home:
        try:
            rel = os.path.relpath(str(resolved), _abspath(home)).replace("\\", "/")
            if not rel.startswith(".."):
                return ("~/" + rel).rstrip("/") or "~"
        except Exception:
            pass
    if root:
        try:
            rel = os.path.relpath(str(resolved), _abspath(root))
            return _safe_display(rel.replace("\\", "/"), resolved, home)
        except Exception:
            pass
    return _safe_display(resolved.name or DEFAULT_HANDOFF, resolved, home)


def _safe_display(text, resolved, home):
    if _is_etc(resolved) or _is_etc(text):
        return "outside"
    if _USERS_DIR.search(text) or _POSIX_HOME.search(text):
        return "<home>"
    if _under_users_root(resolved):
        if home:
            try:
                Path(resolved).resolve().relative_to(Path(home).resolve())
                return text
            except Exception:
                return "outside"
        return "outside"
    return text or DEFAULT_HANDOFF


def _read_json(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def rejected_write_keys(data):
    if not isinstance(data, dict):
        return []
    return sorted(key for key in data if key in WRITE_KEYS)


def _has_dotdot(raw):
    return ".." in Path(str(raw)).parts


def resolve_handoff(root, environ=None, config_file=None):
    """Read-only handoff folder. control.local.json first; HBC_HANDOFF is hub fallback.

    Write keys in the config are rejected and ignored. Approvals logs stay at
    the project root, never inside the configured handoff. A `..` component
    in the setting is rejected and the lab bus/ default is used.
    """
    root = Path(root).resolve()
    source = os.environ if environ is None else environ
    env_value = str(source.get(ENV_HANDOFF) or "").strip()
    data = {}
    cfg = Path(config_file) if config_file else config_path(root)
    if cfg.is_file():
        data = _read_json(cfg)
    rejected = rejected_write_keys(data)
    config_value = str(data.get("handoff") or "").strip()
    config_dotdot = bool(config_value and _has_dotdot(config_value))
    if config_dotdot:
        rejected = sorted(set(rejected) | {"handoff"})
        config_value = ""
    env_dotdot = bool(env_value and _has_dotdot(env_value))
    if env_dotdot:
        env_value = ""
    if config_value:
        raw, origin = config_value, "config"
    elif env_value:
        raw, origin = env_value, "env"
    else:
        raw, origin = DEFAULT_HANDOFF, "default"
    path = Path(raw)
    if not path.is_absolute():
        path = (root / path).resolve()
    else:
        path = path.resolve()
    approvals = root / "approvals.jsonl"
    display = (
        REJECTED_DISPLAY
        if origin == "default" and (config_dotdot or env_dotdot)
        else display_handoff(path, root=root)
    )
    return {
        "handoff": path,
        "display": display,
        "source": origin,
        "rejected_write_keys": rejected,
        "writable": False,
        "approvals": approvals,
    }


def approvals_path(root):
    """Always the project-root log. Never the configured handoff folder."""
    return Path(root).resolve() / "approvals.jsonl"


def assert_not_handoff_write(target, handoff):
    """Raise if a write target would land inside the configured handoff."""
    try:
        Path(target).resolve().relative_to(Path(handoff).resolve())
    except Exception:
        return
    raise ValueError("refused write into handoff")
