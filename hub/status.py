"""Read-only GET /status. Loopback only. No new port. Does not touch signing."""

import asyncio
import ctypes
import hmac
import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from hub.control_config import approvals_path, resolve_handoff
from hub.events import EVENT_LOG_NAME, event_log_path, read_last_events
from hub.jobs import held_from_approvals, ready_queue
from hub.tailread import TAIL_BYTES, read_tail
from hub.usage import usage_rows

LOOPBACK = ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")
DEFAULT_HUB_PORT = 8107
HUB_PORT_MIN = 1024
HUB_PORT_MAX = 65535
# Default-port snapshot. Live checks use hub_port(request) / HBC_HUB_PORT.
ALLOWED_ORIGIN = "http://127.0.0.1:8107"
RESTART_BUILD_PATH = "/api/restart-build"
BUILD_PANE = "build"
# Hard-coded until BUILD-CLAIM (#9). No env or config switch.
BUILD_CLAIM_READY = False
HELD_UNTIL_BUILD_CLAIM = "held until BUILD-CLAIM"
SESSIONS_NAME = ".sessions.json"
OPEN_BUILD_NAME = ".open-build.json"
SESSIONS_MAX_BYTES = 64 * 1024
OPEN_BUILD_KIND = "open_build"
# #6 (2f952ae on integration 168ff6b) stores {sessions, open_build} in
# .sessions.json. This branch keeps server.sessions()/remember() as a list
# of job rows and holds open_build in .open-build.json. load_sessions_store
# reads both (#6 adapter). Later rebase onto 168ff6b: drop the side file
# write and call #6 remember_open_build/forget_open_build.
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
ERROR_ACCESS_DENIED = 5
HANDLE = ctypes.c_void_p
DWORD = ctypes.c_uint32
BOOL = ctypes.c_int
NTSTATUS = ctypes.c_long

_signing_cache = None


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Reserved1", ctypes.c_void_p),
        ("PebBaseAddress", ctypes.c_void_p),
        ("Reserved2", ctypes.c_void_p * 2),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
    ]


def reset_signing_cache():
    global _signing_cache
    _signing_cache = None


def _header(request, name):
    headers = getattr(request, "headers", None)
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    if getter is None:
        return ""
    return str(getter(name, "") or getter(name.lower(), "") or "")


def client_is_loopback(request):
    """True only for the connected peer. Ignores forwarded headers."""
    host = None
    transport = getattr(request, "transport", None)
    if transport is not None:
        peer = transport.get_extra_info("peername")
        if peer:
            host = peer[0]
    if not host:
        host = getattr(request, "remote", None)
    return host in LOOPBACK


def hub_port(request=None):
    """Active hub port. request.app['hbc_port'] from make_app; else the default."""
    if request is not None:
        app = getattr(request, "app", None)
        getter = getattr(app, "get", None) if app is not None else None
        if getter is not None:
            value = getter("hbc_port")
            if value is not None:
                try:
                    port = int(value)
                except (TypeError, ValueError):
                    port = None
                else:
                    if HUB_PORT_MIN <= port <= HUB_PORT_MAX:
                        return port
    return DEFAULT_HUB_PORT


def lab_origin(request=None):
    return "http://127.0.0.1:%s" % hub_port(request)


def lab_hosts(request=None):
    port = hub_port(request)
    return ("127.0.0.1:%s" % port, "localhost:%s" % port)


def lab_bind(request=None, port=None):
    if port is None:
        port = hub_port(request)
    return "127.0.0.1:%s" % int(port)


def origin_allowed(request):
    """Same Origin rule as server.allowed(): empty is ok; otherwise exact lab origin."""
    origin = _header(request, "Origin")
    if origin and origin != lab_origin(request):
        return False
    return True


def host_allowed(request):
    host = _header(request, "Host").strip().lower()
    return host in lab_hosts(request)


def request_allowed(request):
    return client_is_loopback(request) and origin_allowed(request) and host_allowed(request)


def _bus_stamp(bus_dir):
    items = []
    bus_dir = Path(bus_dir)
    try:
        if not bus_dir.is_dir():
            return ()
        for path in sorted(bus_dir.glob("*.md")):
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append((path.name, stat.st_mtime_ns, stat.st_size))
    except OSError:
        return ()
    return tuple(items)


def signing_mode(bus_dir):
    """v1/v2 from a 64 KB tail. Re-read on mtime/size change, not on a time cache."""
    global _signing_cache
    stamp = _bus_stamp(bus_dir)
    if _signing_cache is not None:
        mode, v1, cached = _signing_cache
        if cached == stamp:
            return mode, v1
    v1 = False
    bus_dir = Path(bus_dir)
    try:
        if bus_dir.is_dir():
            for path in sorted(bus_dir.glob("*.md")):
                text = read_tail(path, max_bytes=TAIL_BYTES)
                for line in text.splitlines():
                    if line.startswith("v: ") and line[3:].strip() == "1":
                        v1 = True
                        break
                if v1:
                    break
    except (OSError, ValueError, UnicodeDecodeError):
        pass
    mode = "v1" if v1 else "v2"
    _signing_cache = (mode, v1, stamp)
    return mode, v1


def build_pane_alive(panes):
    if not panes:
        return False
    pty = None
    try:
        getter = getattr(panes, "get", None)
        pty = getter("build") if getter else None
    except Exception:
        return False
    if pty is None:
        return False
    alive = getattr(pty, "isalive", None)
    try:
        return bool(alive()) if callable(alive) else False
    except Exception:
        return False


def _bind_win_apis(kernel32, ntdll):
    """argtypes/restype for every WinDLL call. HANDLE is c_void_p."""
    kernel32.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
    kernel32.OpenProcess.restype = HANDLE
    kernel32.GetExitCodeProcess.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
    kernel32.GetExitCodeProcess.restype = BOOL
    kernel32.GetProcessTimes.argtypes = [
        HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    kernel32.GetProcessTimes.restype = BOOL
    kernel32.CloseHandle.argtypes = [HANDLE]
    kernel32.CloseHandle.restype = BOOL
    if hasattr(kernel32, "GetLastError"):
        kernel32.GetLastError.argtypes = []
        kernel32.GetLastError.restype = DWORD
    ntdll.NtQueryInformationProcess.argtypes = [
        HANDLE,
        ctypes.c_uint,
        ctypes.c_void_p,
        DWORD,
        ctypes.POINTER(DWORD),
    ]
    ntdll.NtQueryInformationProcess.restype = NTSTATUS
    return kernel32, ntdll


def _win_dlls():
    """kernel32 + ntdll. Raises on non-Windows unless tests mock this."""
    if os.name != "nt":
        raise OSError("Windows ctypes OpenProcess not available")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    return _bind_win_apis(kernel32, ntdll)


def _win_last_error(kernel32):
    getter = getattr(kernel32, "GetLastError", None)
    if callable(getter):
        try:
            return int(getter() or 0)
        except Exception:
            return 0
    try:
        return int(ctypes.get_last_error() or 0)
    except Exception:
        return 0


def _win_open(kernel32, pid):
    """Return (handle, 'ok'|'unknown'|'gone'). Access denied is unknown."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if handle:
        return handle, "ok"
    err = _win_last_error(kernel32)
    handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, int(pid))
    if handle:
        return handle, "ok"
    if err == ERROR_ACCESS_DENIED or _win_last_error(kernel32) == ERROR_ACCESS_DENIED:
        return None, "unknown"
    return None, "gone"


def _win_pid_state(pid, kernel32=None):
    """'alive' | 'dead' | 'unknown'. Never os.kill on Windows."""
    if kernel32 is None:
        kernel32, _ = _win_dlls()
    handle, opened = _win_open(kernel32, pid)
    if opened == "unknown":
        return "unknown"
    if opened == "gone" or not handle:
        return "dead"
    try:
        code = DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return "unknown"
        return "alive" if int(code.value) == STILL_ACTIVE else "dead"
    finally:
        kernel32.CloseHandle(handle)


def _win_pid_alive(pid, kernel32=None):
    state = _win_pid_state(pid, kernel32=kernel32)
    if state == "unknown":
        return None
    return state == "alive"


def _win_pid_identity(pid, kernel32=None, ntdll=None):
    """Create-time + parent, or ('unknown', None) if OpenProcess fails closed."""
    if kernel32 is None or ntdll is None:
        kernel32, ntdll = _win_dlls()
    handle, opened = _win_open(kernel32, pid)
    if opened == "unknown":
        return "unknown"
    if opened == "gone" or not handle:
        return None
    try:
        created = FILETIME()
        exited = FILETIME()
        kernel_t = FILETIME()
        user_t = FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel_t),
            ctypes.byref(user_t),
        ):
            return "unknown"
        create = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        info = PROCESS_BASIC_INFORMATION()
        status_code = ntdll.NtQueryInformationProcess(
            handle, 0, ctypes.byref(info), ctypes.sizeof(info), None
        )
        parent = 0
        if status_code == 0:
            parent = int(info.InheritedFromUniqueProcessId or 0)
        return {"pid": int(pid), "create": create, "parent": parent}
    finally:
        kernel32.CloseHandle(handle)


def _posix_pid_alive(pid):
    try:
        text = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
    except OSError:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    close = text.rfind(")")
    if close >= 0:
        rest = text[close + 1 :].split()
        if rest and rest[0] == "Z":
            return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _posix_pid_identity(pid):
    try:
        text = Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    close = text.rfind(")")
    if close < 0:
        return None
    rest = text[close + 1 :].split()
    if len(rest) < 20:
        return None
    try:
        return {"pid": int(pid), "create": int(rest[19]), "parent": int(rest[1])}
    except (TypeError, ValueError):
        return None


def _pid_alive(pid):
    """True if pid is still the same live process. Windows never uses os.kill."""
    if os.name == "nt":
        return _win_pid_alive(pid)
    return _posix_pid_alive(pid)


def _pid_identity(pid):
    if os.name == "nt":
        return _win_pid_identity(pid)
    return _posix_pid_identity(pid)


def _identity_matches(expected, current):
    """Same pid + create-time. Parent is a hub-ownership claim, not recycle."""
    if not isinstance(expected, dict) or not isinstance(current, dict):
        return False
    try:
        return (
            int(expected.get("pid")) == int(current.get("pid"))
            and expected.get("create") == current.get("create")
        )
    except (TypeError, ValueError):
        return False


def _stored_identity(pty):
    """Spawn-captured identity only. Never infer from a live pid lookup."""
    stored = getattr(pty, "pid_identity", None)
    if not isinstance(stored, dict):
        return None
    try:
        pid = stored.get("pid")
        create = stored.get("create")
        parent = stored.get("parent")
        if pid is None or create is None or parent is None:
            return None
        return {"pid": int(pid), "create": create, "parent": int(parent)}
    except (TypeError, ValueError):
        return None


def attach_pane_identity(pty, hub_pid=None):
    """Store create-time and parent=this hub's pid at spawn.

    Restart compares against this record. Missing identity or a parent
    that is not the hub is a refuse, not an inferred lookup.
    """
    pid = getattr(pty, "pid", None)
    if pid is None:
        return None
    hub = int(hub_pid if hub_pid is not None else os.getpid())
    ident = _pid_identity(int(pid))
    stored = {"pid": int(pid), "create": None, "parent": hub}
    if isinstance(ident, dict) and ident.get("create") is not None:
        stored["create"] = ident["create"]
    try:
        pty.pid_identity = stored
    except Exception:
        pass
    return stored


def _hub_owns(stored, hub_pid):
    if not isinstance(stored, dict):
        return False
    try:
        return int(stored.get("parent")) == int(hub_pid)
    except (TypeError, ValueError):
        return False


def _kill_tree(pid):
    """Kill the process tree. pywinpty has no close/kill; use pty.pid."""
    pid = int(pid)
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    try:
        os.kill(pid, signal.SIGKILL)  # hbc-allow-kill: tree of a pane this hub started
    except OSError:
        pass


def _drop_build(panes):
    try:
        panes.pop(BUILD_PANE, None)
    except Exception:
        return False
    return True


def kill_build_pane(panes, hub_pid=None):
    """Kill only the `build` pane via pty.pid. Confirm dead, then drop.

    Never calls pty.close or pty.kill. Never touches build2..8 or the hub
    process. Requires the spawn-captured identity; missing identity or a
    parent that is not this hub is a refuse. OpenProcess unknown (access
    denied) is also a refuse — the pane stays. A recycled pid is never
    killed.
    """
    closed = []
    if not panes:
        return closed
    getter = getattr(panes, "get", None)
    if getter is None:
        return closed
    try:
        pty = getter(BUILD_PANE)
    except Exception:
        return closed
    if pty is None:
        return closed
    stored = _stored_identity(pty)
    if stored is None:
        return closed
    hub = int(hub_pid if hub_pid is not None else os.getpid())
    if not _hub_owns(stored, hub):
        return closed
    pid = int(stored["pid"])
    pty_pid = getattr(pty, "pid", None)
    if pty_pid is not None:
        try:
            if int(pty_pid) != pid:
                return closed
        except (TypeError, ValueError):
            return closed
    before = _pid_identity(pid)
    if before == "unknown":
        return closed
    alive = _pid_alive(pid)
    if alive is None:
        return closed
    if isinstance(before, dict) and not _identity_matches(stored, before):
        if _drop_build(panes):
            return [BUILD_PANE]
        return closed
    if alive is False:
        if _drop_build(panes):
            return [BUILD_PANE]
        return closed
    try:
        _kill_tree(pid)
    except Exception:
        return closed
    outcome, _after = _confirm_gone(pid, stored)
    if outcome in ("recycled", "dead"):
        if _drop_build(panes):
            return [BUILD_PANE]
        return closed
    return closed


def _confirm_gone(pid, expected, timeout=1.0):
    """Wait until the pid is dead or recycled. Never kill a new occupant.

    Windows never calls waitpid/WNOHANG. It uses the OpenProcess-based
    alive check until timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        if os.name != "nt":
            try:
                os.waitpid(int(pid), os.WNOHANG)
            except (ChildProcessError, OSError, OverflowError):
                pass
        after = _pid_identity(pid)
        if after == "unknown":
            if time.monotonic() >= deadline:
                return "unknown", after
            time.sleep(0.02)
            continue
        if isinstance(after, dict) and expected and not _identity_matches(expected, after):
            return "recycled", after
        alive = _pid_alive(pid)
        if alive is False:
            return "dead", after
        if time.monotonic() >= deadline:
            return ("unknown" if alive is None else "alive"), after
        time.sleep(0.02)


def close_build_panes(panes):
    """Only `build`. build2..8 stay. Kill via pty.pid, never pty.close."""
    return kill_build_pane(panes)


def _empty_sessions():
    return {"sessions": [], "open_build": []}


def _rows_to_store(rows):
    sessions = []
    names = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("kind") == OPEN_BUILD_KIND:
            pane = str(row.get("pane") or "")
            if pane and pane not in names:
                names.append(pane)
            continue
        sessions.append(row)
    return {"sessions": sessions, "open_build": names}


def _read_capped_json(path):
    if not path.is_file():
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > SESSIONS_MAX_BYTES:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        return None


def _open_build_names(value):
    names = []
    if isinstance(value, list):
        items = value
    elif isinstance(value, dict):
        items = value.get("open_build") or []
    else:
        items = []
    for name in items:
        pane = str(name)
        if pane and pane not in names:
            names.append(pane)
    return names


def load_sessions_store(root):
    """Job rows from .sessions.json; open_build from .open-build.json.

    #6 adapter: a {sessions, open_build} dict in .sessions.json (2f952ae
    on 168ff6b) still loads. Leftover kind=open_build list rows are
    split out so remember()/sessions() never see them.
    """
    empty = _empty_sessions()
    path = Path(root) / SESSIONS_NAME
    side = Path(root) / OPEN_BUILD_NAME
    sessions = []
    names = []
    data = _read_capped_json(path)
    if isinstance(data, list):
        parsed = _rows_to_store(data)
        sessions = parsed["sessions"]
        names = parsed["open_build"]
    elif isinstance(data, dict):
        jobs = data.get("sessions") if isinstance(data.get("sessions"), list) else []
        sessions = [
            row
            for row in jobs
            if isinstance(row, dict) and row.get("kind") != OPEN_BUILD_KIND
        ]
        names = _open_build_names(data.get("open_build"))
    side_data = _read_capped_json(side)
    if side_data is not None:
        names = _open_build_names(side_data)
    if not sessions and not names:
        return empty if data is None and side_data is None else {
            "sessions": sessions,
            "open_build": names,
        }
    return {"sessions": sessions, "open_build": names}


def write_sessions_store(root, store):
    """Job-row list in .sessions.json; pane names in .open-build.json.

    open_build stays out of remember()'s 20-row trim. Later rebase onto
    168ff6b: drop the side-file write and call #6 remember_open_build /
    forget_open_build.
    """
    path = Path(root) / SESSIONS_NAME
    side = Path(root) / OPEN_BUILD_NAME
    rows = []
    for row in list(store.get("sessions") or [])[-20:]:
        if isinstance(row, dict) and row.get("kind") != OPEN_BUILD_KIND:
            rows.append(row)
    path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    names = _open_build_names(store.get("open_build"))
    side.write_text(json.dumps(names, indent=2), encoding="utf-8")


def remember_open_build(root, pane):
    if pane != BUILD_PANE and not (
        isinstance(pane, str) and pane.startswith("build") and pane[5:].isdigit()
    ):
        return
    store = load_sessions_store(root)
    if pane in store["open_build"]:
        return
    store["open_build"].append(pane)
    write_sessions_store(root, store)


def forget_open_build(root, pane):
    store = load_sessions_store(root)
    if pane not in store["open_build"]:
        return
    store["open_build"] = [name for name in store["open_build"] if name != pane]
    write_sessions_store(root, store)


def apply_restart_kill(panes, root):
    """Kill `build`, require a non-empty closed list, then forget open_build."""
    closed = kill_build_pane(panes)
    if not closed:
        return {"ok": False, "error": "empty closed", "closed": []}
    try:
        forget_open_build(root, BUILD_PANE)
    except Exception:
        pass
    return {"ok": True, "closed": closed}


def build_claim_state(root=None):
    """None when BUILD-CLAIM is not ready or the claim cannot be read. Fail safe."""
    if not BUILD_CLAIM_READY:
        return None
    return None


def restart_held_response():
    return web.json_response(
        {"ok": False, "error": HELD_UNTIL_BUILD_CLAIM},
        status=409,
    )


def status_payload(root, now=None, events_limit=10, panes=None, environ=None, port=None):
    """Read-only snapshot. Never calls queue() or any writer."""
    root = Path(root)
    cfg = resolve_handoff(root, environ=environ)
    handoff = cfg["handoff"]
    if now is None:
        now = datetime.now(timezone.utc).astimezone()
    stamp = now.isoformat(timespec="seconds")
    try:
        mode, v1 = signing_mode(handoff)
    except Exception:
        mode, v1 = "UNKNOWN", False
    try:
        event_rows = read_last_events(event_log_path(root), limit=events_limit)
    except Exception:
        event_rows = []
    try:
        usage = usage_rows(now=now)
    except Exception:
        usage = []
    try:
        held = held_from_approvals(approvals_path(root))
    except Exception:
        held = []
    try:
        ready = ready_queue(handoff, approvals=approvals_path(root), root=root)
    except Exception:
        ready = []
    return {
        "hub": "up",
        "bind": lab_bind(port=port if port is not None else DEFAULT_HUB_PORT),
        "signing": mode,
        "v1_found": v1,
        "time": stamp,
        "usage": usage,
        "events": event_rows,
        "events_path": EVENT_LOG_NAME,
        "handoff": cfg["display"],
        "build_pane": build_pane_alive(panes),
        "held": held,
        "ready": ready,
        "restart_build": BUILD_CLAIM_READY,
        "staging": False,
    }


def refused():
    return web.json_response({"error": "refused"}, status=403)


async def handle_status(request):
    if not request_allowed(request):
        return refused()
    root = Path(request.app.get("hbc_root") or ".")
    panes = request.app.get("hbc_panes")
    port = hub_port(request)
    try:
        payload = await asyncio.to_thread(status_payload, root, None, 10, panes, None, port)
    except Exception:
        payload = {
            "hub": "UNKNOWN",
            "bind": lab_bind(port=port),
            "signing": "UNKNOWN",
            "v1_found": False,
            "usage": [],
            "events": [],
            "events_path": EVENT_LOG_NAME,
            "handoff": "bus",
            "build_pane": False,
            "held": [],
            "ready": [],
            "restart_build": BUILD_CLAIM_READY,
            "staging": False,
        }
    fn = request.app.get("hbc_is_staging")
    if callable(fn):
        try:
            payload["staging"] = bool(fn())
        except Exception:
            payload["staging"] = False
    return web.json_response(payload)


def _token_ok(request):
    expected = str(request.app.get("hbc_token") or "")
    if not expected:
        return False
    headers = getattr(request, "headers", None)
    query = getattr(request, "query", None)
    supplied = ""
    if query is not None:
        getter = getattr(query, "get", None)
        if getter:
            supplied = str(getter("token") or "")
    if not supplied and headers is not None:
        getter = getattr(headers, "get", None)
        if getter:
            supplied = str(getter("X-Hbc-Token") or getter("x-hbc-token") or "")
    try:
        return hmac.compare_digest(supplied, expected)
    except Exception:
        return False


async def handle_restart_build(request):
    """Restart the hub `build` pane. Loopback + token. Never the hub PID.

    Held until BUILD-CLAIM: mid-job is claimed-and-not-returned, or a
    running marker the server checks too. If the claim cannot be read,
    refuse.
    """
    if not request_allowed(request):
        return refused()
    if not _token_ok(request):
        return refused()
    claim = build_claim_state(request.app.get("hbc_root"))
    if not BUILD_CLAIM_READY or claim is None:
        return restart_held_response()
    if claim.get("claimed") and not claim.get("returned"):
        return restart_held_response()
    panes = request.app.get("hbc_panes")
    root = request.app.get("hbc_root") or "."
    try:
        result = apply_restart_kill(panes, root)
    except Exception:
        result = {"ok": False, "error": "empty closed", "closed": []}
    if not result.get("ok") or not result.get("closed"):
        return web.json_response(
            {"ok": False, "error": result.get("error") or "empty closed", "closed": []},
            status=409,
        )
    return web.json_response(result)
