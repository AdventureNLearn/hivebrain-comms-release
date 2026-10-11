#!/usr/bin/env python3
"""PC-runnable scrub check. Not a bus job.

pane: build is BAD_PANE on run_pane, and hub approve runs grok in plan
mode, so a deterministic probe cannot ride the bus. This check uses a
temp state folder (never the live hub folder), runs the probe through
the real run_pane subprocess path (child swapped to the hub
interpreter via sys.executable and set_test_grok_argv), then
apply_decision so the return file, .sessions.json, and /api/state
payload are written in that temp folder.

Prints PASS or FAIL for probe-line-3161 to 3200, <truncated>, <home>,
<user>, and no raw home or username. Exit 0 or 1.

Run from the hub repo root with the hub's Python:

    <hub python> -m tools.scrub_e2e
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

PROBE = REPO / "tools" / "scrub-probe.py"
FAKE_USER = "wxyz"
EXPECTED_LINES = ["probe-line-%04d" % index for index in range(3161, 3201)]


def _probe_argv():
    """Use the hub interpreter the harness already launched."""
    return [sys.executable, str(PROBE)]


def _write_fake_grok(directory, source):
    directory = Path(directory)
    script = directory / "fake-grok.py"
    script.write_text(source, encoding="utf-8")
    if os.name == "nt":
        grok = directory / "fake-grok.cmd"
        grok.write_text(
            '@echo off\r\n"{exe}" "{script}" %*\r\n'.format(
                exe=sys.executable,
                script=script,
            ),
            encoding="utf-8",
        )
        return grok
    grok = directory / "fake-grok"
    grok.write_text("#!%s\n%s" % (sys.executable, source), encoding="utf-8")
    grok.chmod(grok.stat().st_mode | stat.S_IEXEC)
    return grok


def _uses_protocol(server):
    return getattr(server, "protocol", None) is not None


def _lane(server):
    inbox, owner = next(iter(server.OWNERS.items()))
    return inbox, owner, server.RETURNS[owner]


def _job_fields(server, owner):
    if _uses_protocol(server):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        iso = datetime.now(ZoneInfo("America/New_York")).replace(microsecond=0).isoformat()
        time_s, routed = iso, owner + " " + iso
    else:
        time_s, routed = "2026-10-10 12:00", "2026-10-10 12:01"
    return {
        "v": "2",
        "id": "scrub-e2e-1",
        "from": owner,
        "to": "Build",
        "lane": "lab",
        "priority": "normal",
        "time": time_s,
        "routed": routed,
        "pane": "1",
        "job": "run the scrub probe",
        "done-when": "the probe exits 1 and the hub stores a scrubbed tail",
    }


def _sign_and_write(bus, fields, key, inbox):
    from hub import sigverify

    signed = dict(fields)
    signed["sig"] = sigverify.mac_hex(fields, key)
    names = (
        "id", "v", "from", "to", "lane", "priority", "time", "routed",
        "pane", "job", "done-when", "sig",
    )
    (bus / inbox).write_text(
        "\n".join("%s: %s" % (name, signed[name]) for name in names) + "\n",
        encoding="utf-8",
    )
    return signed


def _report(name, ok):
    print(("PASS" if ok else "FAIL") + " " + name)
    return bool(ok)


def _contains_raw(blob, fake_home, fake_user):
    text = blob if isinstance(blob, str) else json.dumps(blob)
    if not text:
        return False
    lowered = text.lower()
    if fake_user and fake_user.lower() in lowered:
        return True
    if fake_home and fake_home.lower() in lowered:
        return True
    return False


def main():
    if not PROBE.is_file():
        print("FAIL probe-line-3161 to 3200")
        print("FAIL <truncated>")
        print("FAIL <home>")
        print("FAIL <user>")
        print("FAIL no raw home or username")
        print("error: missing tools/scrub-probe.py")
        return 1

    from hub import sigverify
    import server

    # Import and apply_decision only. Never start_hub / ensure_session_token.
    live_root = Path(server.ROOT).resolve()
    live_token = live_root / ".session-token"
    live_token_before = live_token.read_bytes() if live_token.is_file() else None
    tmp = tempfile.TemporaryDirectory(prefix="hbc-scrub-e2e-")
    captured = {}
    key = bytearray(os.urandom(32))
    prev = {}
    real_run_pane = server.run_pane
    ok_lines = ok_trunc = ok_home = ok_user = ok_raw = False
    try:
        home = Path(tmp.name).resolve()
        if live_root == home or live_root in home.parents or home == live_root:
            raise RuntimeError("temp state folder resolved to the live hub folder")
        fake_home = home / "hbchome"
        fake_home.mkdir()
        bus = home / "bus"
        bus.mkdir()
        inbox, owner, outbox = _lane(server)
        for name in set(list(server.OWNERS) + list(server.RETURNS.values())):
            (bus / name).write_text("", encoding="utf-8")

        grok = _write_fake_grok(
            home,
            "import os, subprocess\n"
            "env = dict(os.environ)\n"
            "env['HOME'] = %r\n"
            "env['USERPROFILE'] = %r\n"
            "env['USERNAME'] = %r\n"
            "env['USER'] = %r\n"
            "raise SystemExit(subprocess.call(%r, env=env))\n"
            % (
                str(fake_home),
                str(fake_home),
                FAKE_USER,
                FAKE_USER,
                _probe_argv(),
            ),
        )

        prev["ROOT"] = server.ROOT
        prev["BUS"] = server.BUS
        prev["LOG"] = server.LOG
        prev["SEEN"] = server.SEEN
        prev["SESSIONS"] = server.SESSIONS
        prev["EVENTS"] = getattr(server, "EVENTS", None)
        prev["GROK"] = server.GROK
        prev["broken"] = getattr(server, "_sessions_broken", False)

        server.ROOT = home
        server.BUS = bus
        server.LOG = home / "approvals.jsonl"
        server.SEEN = home / ".seen-ids.txt"
        server.SESSIONS = home / ".sessions.json"
        if prev["EVENTS"] is not None:
            server.EVENTS = home / ".hub-events.jsonl"
        server.GROK = grok
        server._sessions_broken = False
        server.set_run_impl(None)
        server.set_test_grok_argv(True)

        for path in (server.ROOT, server.BUS, server.LOG.parent, server.SEEN.parent, server.SESSIONS.parent):
            resolved = Path(path).resolve()
            if live_root == resolved or live_root in resolved.parents:
                raise RuntimeError("refused to point state at the live hub folder")

        if _uses_protocol(server):
            server.protocol.set_approvals_log(server.LOG)
            server.protocol.read_seen(server.SEEN)

        def wrapped(pane, command, fields=None, owner=None):
            code, out, err = real_run_pane(pane, command, fields, owner)
            captured["code"] = code
            captured["out"] = out
            captured["err"] = err
            return code, out, err

        server.run_pane = wrapped
        sigverify.set_key_provider(lambda name: bytearray(key) if name == owner else None)
        _sign_and_write(bus, _job_fields(server, owner), key, inbox)

        env = {
            "HOME": str(fake_home),
            "USERPROFILE": str(fake_home),
            "USERNAME": FAKE_USER,
            "USER": FAKE_USER,
        }
        os.environ.update(env)
        status, payload = server.apply_decision("scrub-e2e-1", "approve")
        state = server.state_payload()

        returned = (bus / outbox).read_text(encoding="utf-8")
        sessions_text = ""
        if server.SESSIONS.is_file():
            sessions_text = server.SESSIONS.read_text(encoding="utf-8")
        stored = "\n".join((
            returned,
            json.dumps(payload),
            json.dumps(state),
            sessions_text,
            captured.get("out") or "",
        ))
        err = captured.get("err") or ""
        out = captured.get("out") or ""

        ok_lines = (
            status == 200
            and captured.get("code") == 1
            and all(line in stored for line in EXPECTED_LINES)
        )
        ok_trunc = "<truncated>" in stored
        ok_home = "probe-home=<home>" in err and "<home>" in err
        ok_user = "probe-user=<user>" in err and "<user>" in err
        blobs = (returned, sessions_text, json.dumps(payload), json.dumps(state), out, err)
        ok_raw = all(not _contains_raw(blob, str(fake_home), FAKE_USER) for blob in blobs)
        if status != 200:
            err_name = ""
            if isinstance(payload, dict):
                err_name = str(payload.get("error") or "")
            if err_name:
                print("error: apply_decision %s %s" % (status, err_name))
    except Exception as exc:
        print("error: %s" % type(exc).__name__)
    finally:
        server.run_pane = real_run_pane
        server.set_run_impl(None)
        server.set_test_grok_argv(False)
        if prev.get("GROK") is not None:
            server.GROK = prev["GROK"]
        if prev.get("ROOT") is not None:
            server.ROOT = prev["ROOT"]
            server.BUS = prev["BUS"]
            server.LOG = prev["LOG"]
            server.SEEN = prev["SEEN"]
            server.SESSIONS = prev["SESSIONS"]
            if prev.get("EVENTS") is not None:
                server.EVENTS = prev["EVENTS"]
        if "broken" in prev:
            server._sessions_broken = prev["broken"]
        sigverify.clear_key_provider()
        sigverify._wipe(key)
        tmp.cleanup()
        live_token_after = live_token.read_bytes() if live_token.is_file() else None
        if live_token_after != live_token_before:
            print("error: live .session-token changed")
            ok_raw = False

    all_ok = True
    all_ok = _report("probe-line-3161 to 3200", ok_lines) and all_ok
    all_ok = _report("<truncated>", ok_trunc) and all_ok
    all_ok = _report("<home>", ok_home) and all_ok
    all_ok = _report("<user>", ok_user) and all_ok
    all_ok = _report("no raw home or username", ok_raw) and all_ok
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
