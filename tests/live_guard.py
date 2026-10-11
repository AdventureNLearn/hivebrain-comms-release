"""Refuse writes to the live hub .sessions.json, .hub-events.jsonl, and .hub-process."""

from pathlib import Path

_installed = False


def live_hub_paths():
    import server

    root = Path(server.__file__).resolve().parent
    return (
        (root / ".sessions.json").resolve(),
        (root / ".hub-events.jsonl").resolve(),
        (root / ".hub-process").resolve(),
    )


def point_hub_at(home):
    """Point ROOT, HUB_PROCESS, and HANDOFF at a temp folder. Return the previous values."""
    import server

    home = Path(home)
    prev = {
        "ROOT": server.ROOT,
        "HUB_PROCESS": server.HUB_PROCESS,
        "HANDOFF": getattr(server, "HANDOFF", None),
    }
    server.ROOT = home
    server.HUB_PROCESS = home / ".hub-process"
    server.HANDOFF = home / "handoff"
    return prev


def restore_hub(prev):
    import server

    server.ROOT = prev["ROOT"]
    server.HUB_PROCESS = prev["HUB_PROCESS"]
    if prev.get("HANDOFF") is not None:
        server.HANDOFF = prev["HANDOFF"]


def install_live_state_guard():
    """Install once. Safe to call from tests/__init__.py and each test module."""
    global _installed
    if _installed:
        return
    import server

    live_sessions, live_events, live_hub = live_hub_paths()
    real_atomic_json = server._atomic_write_json
    real_atomic = server._atomic_write
    real_log = server.log_hub_event
    real_write_hub = server.write_hub_process

    def guarded_atomic_json(path, payload):
        resolved = Path(path).resolve()
        if resolved == live_sessions:
            raise AssertionError("refused to write live .sessions.json")
        if resolved == live_events:
            raise AssertionError("refused to write live .hub-events.jsonl")
        if resolved == live_hub:
            raise AssertionError("refused to write live .hub-process")
        return real_atomic_json(path, payload)

    def guarded_atomic(path, text):
        resolved = Path(path).resolve()
        if resolved == live_hub:
            raise AssertionError("refused to write live .hub-process")
        if resolved == live_sessions:
            raise AssertionError("refused to write live .sessions.json")
        if resolved == live_events:
            raise AssertionError("refused to write live .hub-events.jsonl")
        return real_atomic(path, text)

    def guarded_log(*args, **kwargs):
        if Path(server.EVENTS).resolve() == live_events:
            raise AssertionError("refused to write live .hub-events.jsonl")
        return real_log(*args, **kwargs)

    def guarded_write_hub():
        if Path(server.hub_process_path()).resolve() == live_hub:
            raise AssertionError("refused to write live .hub-process")
        return real_write_hub()

    server._atomic_write_json = guarded_atomic_json
    server._atomic_write = guarded_atomic
    server.log_hub_event = guarded_log
    server.write_hub_process = guarded_write_hub
    live_handoff = (Path.home() / ".grok" / "handoff").resolve()
    current_handoff = getattr(server, "HANDOFF", None)
    if current_handoff is not None:
        try:
            if Path(current_handoff).resolve() == live_handoff:
                server.HANDOFF = Path(server.__file__).resolve().parent / ".test-handoff-unused"
        except OSError:
            server.HANDOFF = Path(server.__file__).resolve().parent / ".test-handoff-unused"
    _installed = True
