"""Importing server must not mint or rewrite ROOT/.session-token."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server
from tests.live_guard import install_live_state_guard, point_hub_at, restore_hub

install_live_state_guard()

LIVE_TOKEN = ROOT / ".session-token"
SENTINEL = "sentinel-session-token-do-not-change\n"


def _live_token_bytes():
    if not LIVE_TOKEN.is_file():
        return None
    return LIVE_TOKEN.read_bytes()


class TestSessionToken(unittest.TestCase):
    def test_import_server_neither_creates_nor_changes_session_token(self):
        live_before = _live_token_bytes()
        tmp = tempfile.TemporaryDirectory()
        try:
            work = Path(tmp.name)
            shutil.copy(ROOT / "server.py", work / "server.py")
            token_path = work / ".session-token"
            token_path.write_text(SENTINEL, encoding="utf-8")
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join((str(work), str(ROOT), env.get("PYTHONPATH") or ""))
            script = (
                "import server\n"
                "from pathlib import Path\n"
                "p = Path(server.__file__).resolve().parent / '.session-token'\n"
                "print('EXISTS=' + str(p.is_file()))\n"
                "print('TEXT=' + (p.read_text(encoding='utf-8') if p.is_file() else ''))\n"
                "print('TOKEN=' + repr(server.TOKEN))\n"
            )
            created = subprocess.run(
                [sys.executable, "-c", script],
                cwd=str(work),
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(token_path.read_text(encoding="utf-8"), SENTINEL)
            self.assertIn("EXISTS=True", created.stdout)
            self.assertIn("TEXT=" + SENTINEL, created.stdout)
            self.assertIn("TOKEN=''", created.stdout)

            token_path.unlink()
            missing = subprocess.run(
                [sys.executable, "-c", script],
                cwd=str(work),
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(missing.returncode, 0, missing.stderr)
            self.assertFalse(token_path.exists())
            self.assertIn("EXISTS=False", missing.stdout)
            self.assertIn("TOKEN=''", missing.stdout)
        finally:
            tmp.cleanup()
            self.assertEqual(_live_token_bytes(), live_before)

    def test_ensure_session_token_writes_under_rebound_root(self):
        live_before = _live_token_bytes()
        tmp = tempfile.TemporaryDirectory()
        prev_root = server.ROOT
        prev_token = server.TOKEN
        try:
            home = Path(tmp.name)
            server.ROOT = home
            server.TOKEN = ""
            server.ensure_session_token()
            path = home / ".session-token"
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_text(encoding="utf-8"), server.TOKEN)
            self.assertTrue(server.TOKEN)
        finally:
            server.ROOT = prev_root
            server.TOKEN = prev_token
            tmp.cleanup()
            self.assertEqual(_live_token_bytes(), live_before)

    def test_start_hub_mints_token_under_rebound_root(self):
        live_before = _live_token_bytes()
        tmp = tempfile.TemporaryDirectory()
        prev_hub = point_hub_at(tmp.name)
        prev_token = server.TOKEN
        prev_events = getattr(server, "EVENTS", None)
        try:
            home = Path(tmp.name)
            server.TOKEN = ""
            if prev_events is not None:
                server.EVENTS = home / ".hub-events.jsonl"

            class Runner:
                def __init__(self, app):
                    self.app = app

                async def setup(self):
                    return None

                async def cleanup(self):
                    return None

            class Site:
                def __init__(self, *args, **kwargs):
                    return None

                async def start(self):
                    return None

            with patch.object(web, "AppRunner", Runner), patch.object(web, "TCPSite", Site), patch.object(
                server, "after_port_bound", lambda: None
            ):
                asyncio.run(server.start_hub())
            path = home / ".session-token"
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_text(encoding="utf-8"), server.TOKEN)
            self.assertTrue(server.TOKEN)
        finally:
            restore_hub(prev_hub)
            server.TOKEN = prev_token
            if prev_events is not None:
                server.EVENTS = prev_events
            tmp.cleanup()
            self.assertEqual(_live_token_bytes(), live_before)

    def test_allowed_refuses_empty_missing_whitespace_while_token_blank(self):
        prev = server.TOKEN
        try:
            server.TOKEN = ""

            class Req:
                def __init__(self, path="/api/state", query=None, headers=None):
                    self.path = path
                    self.query = query or {}
                    self.headers = headers or {}

            self.assertFalse(server.allowed(Req()))
            self.assertFalse(server.allowed(Req(query={"token": ""})))
            self.assertFalse(server.allowed(Req(query={"token": " "})))
            self.assertFalse(server.allowed(Req(query={"token": "   "})))
            self.assertFalse(server.allowed(Req(headers={"X-Hbc-Token": ""})))
            self.assertFalse(server.allowed(Req(headers={"X-Hbc-Token": " "})))
            self.assertFalse(server.allowed(Req(headers={"X-Hbc-Token": "\t"})))
        finally:
            server.TOKEN = prev

    def test_claim_reply_mac_refuses_empty_key(self):
        if not hasattr(server, "claim_reply_mac"):
            self.skipTest("claim_reply_mac is not on this branch")
        prev = server.TOKEN
        try:
            server.TOKEN = ""
            with self.assertRaises(ValueError):
                server.claim_reply_mac("id-1", "nonce-1", "GO")
            with self.assertRaises(ValueError):
                server.claim_reply_mac("id-1", "nonce-1", "GO", token="")
            mac = server.claim_reply_mac("id-1", "nonce-1", "GO", token="lab-token")
            self.assertTrue(mac)
            self.assertEqual(len(mac), 64)
        finally:
            server.TOKEN = prev

    def test_start_hub_mints_before_bind(self):
        live_before = _live_token_bytes()
        tmp = tempfile.TemporaryDirectory()
        prev_hub = point_hub_at(tmp.name)
        prev_token = server.TOKEN
        prev_events = getattr(server, "EVENTS", None)
        order = []
        try:
            home = Path(tmp.name)
            server.TOKEN = ""
            if prev_events is not None:
                server.EVENTS = home / ".hub-events.jsonl"

            def mint():
                order.append("mint")
                server.TOKEN = "a" * 32
                (home / ".session-token").write_text(server.TOKEN, encoding="utf-8")

            class Runner:
                def __init__(self, app):
                    self.app = app

                async def setup(self):
                    order.append("setup")

                async def cleanup(self):
                    return None

            class Site:
                def __init__(self, *args, **kwargs):
                    order.append("site")

                async def start(self):
                    order.append("bind")
                    if not server.TOKEN:
                        raise AssertionError("TOKEN was empty at bind")

            with patch.object(server, "ensure_session_token", mint), patch.object(
                web, "AppRunner", Runner
            ), patch.object(web, "TCPSite", Site), patch.object(
                server, "after_port_bound", lambda: None
            ):
                asyncio.run(server.start_hub())
            self.assertIn("mint", order)
            self.assertIn("bind", order)
            self.assertLess(order.index("mint"), order.index("bind"))
            self.assertEqual(order[0], "mint")
        finally:
            restore_hub(prev_hub)
            server.TOKEN = prev_token
            if prev_events is not None:
                server.EVENTS = prev_events
            tmp.cleanup()
            self.assertEqual(_live_token_bytes(), live_before)

    def test_claim_refuses_non_localhost_and_missing_token(self):
        if not hasattr(server, "claim"):
            self.skipTest("/api/claim is not on this branch")
        prev = server.TOKEN
        try:
            server.TOKEN = ""

            class Req:
                def __init__(self, remote="127.0.0.1", host="127.0.0.1:8107", token=None):
                    self.remote = remote
                    self.transport = None
                    self.path = "/api/claim"
                    self.query = {}
                    headers = {"Host": host}
                    if token is not None:
                        headers["X-Hbc-Token"] = token
                    self.headers = headers

                async def json(self):
                    return {"id": "claim-1", "sig": "x", "nonce": "n"}

            async def run(req):
                resp = await server.claim(req)
                body = resp.body
                if isinstance(body, (bytes, bytearray)):
                    payload = json.loads(body.decode("utf-8"))
                else:
                    payload = json.loads(body)
                return resp.status, payload

            status, payload = asyncio.run(run(Req(remote="8.8.8.8")))
            self.assertEqual(status, 403)
            self.assertEqual(payload.get("verdict"), "REFUSED")

            status, payload = asyncio.run(run(Req(remote="127.0.0.1", token="")))
            self.assertEqual(status, 403)
            self.assertEqual(payload.get("verdict"), "REFUSED TOKEN")

            status, payload = asyncio.run(run(Req(remote="127.0.0.1")))
            self.assertEqual(status, 403)
            self.assertEqual(payload.get("verdict"), "REFUSED TOKEN")

            server.TOKEN = "a" * 32
            status, payload = asyncio.run(run(Req(remote="10.0.0.1", token=server.TOKEN)))
            self.assertEqual(status, 403)
            self.assertEqual(payload.get("verdict"), "REFUSED")
        finally:
            server.TOKEN = prev

    def test_claim_valid_token_bad_host_is_403(self):
        if not hasattr(server, "claim"):
            self.skipTest("/api/claim is not on this branch")
        prev = server.TOKEN
        try:
            server.TOKEN = "a" * 32

            class Req:
                def __init__(self, host):
                    self.remote = "127.0.0.1"
                    self.transport = None
                    self.path = "/api/claim"
                    self.query = {}
                    self.headers = {"Host": host, "X-Hbc-Token": server.TOKEN}

                async def json(self):
                    return {"id": "claim-host", "sig": "x", "nonce": "n"}

            async def run(req):
                resp = await server.claim(req)
                body = resp.body
                if isinstance(body, (bytes, bytearray)):
                    payload = json.loads(body.decode("utf-8"))
                else:
                    payload = json.loads(body)
                return resp.status, payload

            for host in (
                "evil.example:8107",
                "example.com",
                "127.0.0.1:9999",
                "127.0.0.1.attacker:8107",
                "8.8.8.8:8107",
                "",
            ):
                status, payload = asyncio.run(run(Req(host)))
                self.assertEqual(status, 403, host)
                self.assertEqual(payload.get("verdict"), "REFUSED", host)
                self.assertNotEqual(payload.get("verdict"), "GO", host)
        finally:
            server.TOKEN = prev

    def test_live_state_guard_refuses_both_live_files(self):
        from tests.live_guard import install_live_state_guard, live_hub_paths

        install_live_state_guard()
        live_sessions, live_events, live_hub = live_hub_paths()
        sess_before = live_sessions.read_bytes() if live_sessions.is_file() else None
        ev_before = live_events.read_bytes() if live_events.is_file() else None
        hub_before = live_hub.read_bytes() if live_hub.is_file() else None
        prev_events = server.EVENTS
        try:
            with self.assertRaises(AssertionError) as ctx:
                server._atomic_write_json(live_sessions, {"sessions": [], "open_build": []})
            self.assertIn(".sessions.json", str(ctx.exception))

            with self.assertRaises(AssertionError) as ctx:
                server._atomic_write_json(live_events, {"event": "nope"})
            self.assertIn(".hub-events.jsonl", str(ctx.exception))

            with self.assertRaises(AssertionError) as ctx:
                server._atomic_write(live_hub, "pid=1\nctime=1\n")
            self.assertIn(".hub-process", str(ctx.exception))

            with self.assertRaises(AssertionError) as ctx:
                server.write_hub_process()
            self.assertIn(".hub-process", str(ctx.exception))

            server.EVENTS = live_events
            with self.assertRaises(AssertionError) as ctx:
                server.log_hub_event("guard-probe")
            self.assertIn(".hub-events.jsonl", str(ctx.exception))
        finally:
            server.EVENTS = prev_events
            sess_after = live_sessions.read_bytes() if live_sessions.is_file() else None
            ev_after = live_events.read_bytes() if live_events.is_file() else None
            hub_after = live_hub.read_bytes() if live_hub.is_file() else None
            self.assertEqual(sess_after, sess_before)
            self.assertEqual(ev_after, ev_before)
            self.assertEqual(hub_after, hub_before)

    def test_module_source_has_no_import_time_token_write(self):
        src = Path(server.__file__).read_text(encoding="utf-8")
        self.assertRegex(src, r'(?m)^TOKEN = ""$')
        self.assertNotRegex(src, r'(?m)^TOKEN = secrets\.token_hex')
        before_fn, after_fn = src.split("def ensure_session_token", 1)
        self.assertNotIn("session-token", before_fn.split("def session_token_path", 1)[0])
        self.assertIn("token_hex", after_fn)
        self.assertIn("write_text", after_fn)


if __name__ == "__main__":
    unittest.main(verbosity=2)
