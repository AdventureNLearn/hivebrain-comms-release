"""R5 usage rows. Local sources only. Never cookies, tokens, or private APIs."""

from datetime import datetime, timezone

# Investigated 10 Oct 2026 (this host has no grok binary and no ~/.grok):
# SuperGrok/Grok chat — none. Used/limit/reset live on grok.com Settings → Usage
#   (signed-in page). No CLI status/usage command, no local usage file.
# Imagine generations — none. Same Settings → Usage page; no local source.
# X reads — none. No local X CLI or usage file. Developer APIs need tokens (forbidden).
# X posts — none. Same as X reads.
# Build/grok CLI — none for used/limit/reset. Official CLI reference lists no usage
#   subcommand. Changelog mentions `grok usage` (session token/cost), not the
#   subscription pool. No local usage file here. Do not invoke grok (it may
#   touch credentials internally).
# Grok Heavy — none. No local signal that the account has it; row omitted.
#
# WebView2 scrape is out of scope (app does not read page content).

# No stable public Settings → Usage deep link was found. These are the
# service usage/settings pages. Links must be https on grok.com or x.com.
USAGE_PAGE = {
    "supergrok": "https://grok.com/",
    "imagine": "https://grok.com/imagine",
    "x-reads": "https://x.com/settings",
    "x-posts": "https://x.com/settings",
    "build-cli": "https://grok.com/",
}
ALLOWED_USAGE_HOSTS = ("grok.com", "x.com")

FEATURES = (
    ("supergrok", "SuperGrok/Grok chat", None),
    ("imagine", "Imagine generations", None),
    ("x-reads", "X reads", None),
    ("x-posts", "X posts", None),
    ("build-cli", "Build/grok CLI", None),
)

STALE_AFTER_S = 15 * 60


def _iso(now):
    if now is None:
        now = datetime.now(timezone.utc).astimezone()
    if getattr(now, "tzinfo", None) is None:
        now = now.replace(tzinfo=timezone.utc).astimezone()
    return now, now.isoformat(timespec="seconds")


def usage_rows(now=None, include_heavy=False):
    """One row per feature. No source → UNKNOWN plus that service's usage page."""
    rows = []
    for feature_id, label, source in FEATURES:
        rows.append({
            "id": feature_id,
            "label": label,
            "used": "UNKNOWN",
            "limit": "UNKNOWN",
            "reset": "UNKNOWN",
            "source": "none" if source is None else source,
            "read_at": "UNKNOWN",
            "age": "",
            "usage_page": USAGE_PAGE[feature_id],
        })
    if include_heavy:
        rows.append({
            "id": "heavy",
            "label": "Grok Heavy",
            "used": "UNKNOWN",
            "limit": "UNKNOWN",
            "reset": "UNKNOWN",
            "source": "none",
            "read_at": "UNKNOWN",
            "age": "",
            "usage_page": "https://grok.com/",
        })
    return rows


def allowed_usage_link(url):
    """https only, grok.com and x.com only. Used by the tab before Process.Start."""
    if not url or not isinstance(url, str):
        return False
    text = url.strip()
    if not text.startswith("https://"):
        return False
    rest = text[8:]
    host = rest.split("/", 1)[0].split(":", 1)[0].lower()
    if host in ALLOWED_USAGE_HOSTS:
        return True
    return any(host.endswith("." + allowed) for allowed in ALLOWED_USAGE_HOSTS)


def age_label(read_at, now=None):
    """Friendly age. Usage older than 15 min must show this next to the value."""
    if read_at is None:
        return ""
    if isinstance(read_at, str):
        try:
            read_at = datetime.fromisoformat(read_at)
        except ValueError:
            return ""
    if now is None:
        now = datetime.now(timezone.utc).astimezone()
    if getattr(read_at, "tzinfo", None) is None:
        read_at = read_at.replace(tzinfo=timezone.utc)
    if getattr(now, "tzinfo", None) is None:
        now = now.replace(tzinfo=timezone.utc)
    seconds = int((now - read_at).total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds} s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    return f"{hours} h ago"


def mark_stale(row, now=None):
    """Attach age when the value is older than 15 min. Does not invent numbers."""
    out = dict(row)
    label = age_label(out.get("read_at"), now)
    out["age"] = label
    return out
