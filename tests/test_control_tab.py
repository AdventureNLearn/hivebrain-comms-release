"""SPEC-HBC-CONTROL-TAB-1010 Linux tests. Temporary fixtures only. No secrets."""

import ast
import asyncio
import builtins
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hub import control_actions, control_config, events, jobs, protocol, screen, sigverify, status, tailread, usage
import server
from tests.live_guard import point_hub_at, restore_hub

NEW_PY = (
    ROOT / "hub" / "events.py",
    ROOT / "hub" / "status.py",
    ROOT / "hub" / "usage.py",
    ROOT / "hub" / "jobs.py",
    ROOT / "hub" / "check.py",
    ROOT / "hub" / "control_actions.py",
    ROOT / "hub" / "screen.py",
    ROOT / "hub" / "tailread.py",
    ROOT / "hub" / "control_config.py",
    ROOT / "tests" / "test_control_tab.py",
)
NEW_CS = (
    ROOT / "shell" / "ControlTab.cs",
    ROOT / "shell" / "ControlLogic.cs",
    ROOT / "shell" / "Form1.cs",
)

CRED_APIS = (
    "CryptUnprotectData",
    "ProtectedData",
    "Windows.Security.Credentials",
    "PasswordVault",
    "CredentialCache",
    "CookieContainer",
    "LoadCookies",
    "keyring.get_",
    "secretstorage",
    "libsecret",
    "cookies.sqlite",
    "bus-lead.bin",
    "bus-grokbot.bin",
)

HUGE = 66 * 1024 * 1024


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_huge(path, tail, size=HUGE):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = tail if isinstance(tail, bytes) else tail.encode("utf-8")
    with path.open("wb") as handle:
        handle.truncate(size)
        handle.seek(size)
        handle.write(b"\n")
        handle.write(data)


def _req(remote="127.0.0.1", origin="", host="127.0.0.1:8107", root=None, port=None):
    req = mock.Mock()
    req.transport = None
    req.remote = remote
    req.headers = {"Origin": origin, "Host": host}
    req.app = {"hbc_root": str(root or ROOT)}
    if port is not None:
        req.app["hbc_port"] = port
    return req


def _cs(*names):
    return "\n".join((ROOT / "shell" / name).read_text(encoding="utf-8") for name in names)


class TestStatusRefuse(unittest.TestCase):
    def test_loopback_accepts_ipv4(self):
        req = mock.Mock(transport=None, remote="127.0.0.1")
        self.assertTrue(status.client_is_loopback(req))

    def test_loopback_accepts_ipv6(self):
        req = mock.Mock(transport=None, remote="::1")
        self.assertTrue(status.client_is_loopback(req))

    def test_refuses_non_localhost_remote(self):
        req = mock.Mock(transport=None, remote="8.8.8.8")
        self.assertFalse(status.client_is_loopback(req))

    def test_peername_wins_over_spoofed_remote(self):
        transport = mock.Mock()
        transport.get_extra_info.return_value = ("8.8.8.8", 9)
        req = mock.Mock(transport=transport, remote="127.0.0.1")
        self.assertFalse(status.client_is_loopback(req))

    def test_handler_refuses_non_localhost(self):
        async def run():
            response = await status.handle_status(_req(remote="1.2.3.4"))
            self.assertEqual(response.status, 403)
            body = json.loads(response.body.decode() if isinstance(response.body, bytes) else response.text)
            self.assertEqual(body, {"error": "refused"})

        asyncio.run(run())


class TestStatusOriginAndHost(unittest.IsolatedAsyncioTestCase):
    async def test_empty_origin_and_loopback_host_ok(self):
        response = await status.handle_status(_req(origin="", host="127.0.0.1:8107"))
        self.assertEqual(response.status, 200)

    async def test_lab_origin_ok(self):
        response = await status.handle_status(
            _req(origin="http://127.0.0.1:8107", host="127.0.0.1:8107")
        )
        self.assertEqual(response.status, 200)

    async def test_localhost_host_ok(self):
        response = await status.handle_status(_req(host="localhost:8107"))
        self.assertEqual(response.status, 200)

    async def test_evil_origin_refused(self):
        response = await status.handle_status(
            _req(origin="http://evil.example", host="127.0.0.1:8107")
        )
        self.assertEqual(response.status, 403)

    async def test_https_lab_origin_refused(self):
        response = await status.handle_status(
            _req(origin="https://127.0.0.1:8107", host="127.0.0.1:8107")
        )
        self.assertEqual(response.status, 403)

    async def test_evil_host_refused(self):
        response = await status.handle_status(_req(host="evil.example"))
        self.assertEqual(response.status, 403)

    async def test_wrong_port_host_refused(self):
        response = await status.handle_status(_req(host="127.0.0.1:9999"))
        self.assertEqual(response.status, 403)

    async def test_host_without_port_refused(self):
        response = await status.handle_status(_req(host="127.0.0.1"))
        self.assertEqual(response.status, 403)

    def test_origin_rule_matches_server_allowed(self):
        allowed_src = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn('if origin and origin != f"http://{HOST}:{PORT}"', allowed_src)
        self.assertEqual(status.ALLOWED_ORIGIN, "http://127.0.0.1:8107")
        self.assertFalse(hasattr(status, "ALLOWED_HOSTS"))
        self.assertEqual(status.lab_origin(), "http://127.0.0.1:8107")
        self.assertEqual(status.lab_hosts(), ("127.0.0.1:8107", "localhost:8107"))

    async def test_override_port_origin_and_host(self):
        ok = await status.handle_status(
            _req(origin="http://127.0.0.1:8117", host="127.0.0.1:8117", port=8117)
        )
        self.assertEqual(ok.status, 200)
        payload = json.loads(ok.body.decode() if isinstance(ok.body, bytes) else ok.text)
        self.assertEqual(payload["bind"], "127.0.0.1:8117")
        old_host = await status.handle_status(
            _req(origin="", host="127.0.0.1:8107", port=8117)
        )
        self.assertEqual(old_host.status, 403)
        old_origin = await status.handle_status(
            _req(origin="http://127.0.0.1:8107", host="127.0.0.1:8117", port=8117)
        )
        self.assertEqual(old_origin.status, 403)


class TestStatusHandlerAiohttp(unittest.IsolatedAsyncioTestCase):
    async def test_status_ok_on_loopback_and_403_off(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        app = web.Application()
        app["hbc_root"] = str(ROOT)
        app.router.add_get("/status", status.handle_status)
        async with TestClient(TestServer(app, host="127.0.0.1")) as client:
            ok = await client.get("/status", headers={"Host": "127.0.0.1:8107"})
            self.assertEqual(ok.status, 200)
            payload = await ok.json()
            self.assertEqual(payload["hub"], "up")
            self.assertEqual(payload["bind"], "127.0.0.1:8107")
            self.assertIn(payload["signing"], ("v2", "v1"))
            self.assertEqual(payload["events_path"], ".hub-events.jsonl")
            self.assertIn("build_pane", payload)
            self.assertIn("held", payload)
            self.assertFalse(payload["restart_build"])
            self.assertIn("ready", payload)
            self.assertEqual(payload["handoff"], "bus")

            evil_origin = await client.get(
                "/status",
                headers={"Host": "127.0.0.1:8107", "Origin": "http://evil.example"},
            )
            self.assertEqual(evil_origin.status, 403)

            evil_host = await client.get("/status", headers={"Host": "evil.example"})
            self.assertEqual(evil_host.status, 403)

        refused = await status.handle_status(_req(remote="203.0.113.9"))
        self.assertEqual(refused.status, 403)

    async def test_garbage_event_log_is_200_not_500(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = events.event_log_path(tmp)
            path.write_bytes(b"\xff\xfe not-json\n[1,2,3]\n\"str\"\n")
            response = await status.handle_status(_req(root=tmp))
            self.assertEqual(response.status, 200)
            payload = json.loads(response.body.decode() if isinstance(response.body, bytes) else response.text)
            self.assertEqual(payload["events"], [])

    async def test_payload_raise_is_200_not_500(self):
        err = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")
        with mock.patch("hub.status.status_payload", side_effect=err):
            response = await status.handle_status(_req())
            self.assertEqual(response.status, 200)
            self.assertNotEqual(response.status, 500)


class TestStatusSideEffectFree(unittest.TestCase):
    def test_status_modules_never_call_queue(self):
        status_src = (ROOT / "hub" / "status.py").read_text(encoding="utf-8")
        tree = ast.parse(status_src)
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertNotIn("queue", names)
        server_src = (ROOT / "server.py").read_text(encoding="utf-8")
        start = server_src.index("async def status(")
        end = server_src.index("\nasync def ", start + 1)
        self.assertNotIn("queue(", server_src[start:end])
        self.assertNotIn("hbc_awaiting_approve", server_src[start:end])

    def test_status_payload_is_a_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bus").mkdir()
            payload = status.status_payload(root)
            self.assertEqual(payload["hub"], "up")
            self.assertEqual(payload["events"], [])
            self.assertNotIn("queue", payload)


class TestJobBoardVerdicts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "bus"
        self.home.mkdir()
        self.approvals = self.root / "approvals.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_spoofed_and_duplicate_from_return_verdict(self):
        _write(self.home / "GROKBOT-TO-BUILD.md", (
            "id: probe-spoof\n"
            "from: Lead\n"
            "routed: Grok Bot 2026-10-10T15:05:00-04:00\n"
            "job: list lab files\n"
            "status: returned\n"
            "\n"
            "id: probe-dup\n"
            "from: Grok Bot\n"
            "routed: Grok Bot 2026-10-10T15:06:00-04:00\n"
            "job: list lab files\n"
            "status: picked up\n"
        ))
        _write(self.home / "LEAD-TO-BUILD.md", "")
        _write(self.home / "BUILD-TO-GROKBOT.md", (
            "id: probe-spoof\n"
            "VERDICT: SPOOFED\n"
            "stop\n"
            "\n"
            "id: probe-dup\n"
            "VERDICT: DUPLICATE\n"
            "stop\n"
        ))
        rows = {row["id"]: row for row in jobs.job_board(self.home)}
        self.assertEqual(rows["probe-spoof"]["status"], "SPOOFED")
        self.assertEqual(rows["probe-dup"]["status"], "DUPLICATE")
        self.assertEqual(rows["probe-spoof"]["sender"], "Grok Bot")
        self.assertEqual(rows["probe-spoof"]["routed_et"], "3:05 PM ET")

    def test_approvals_shaped_spoofed_duplicate_denied(self):
        _write(self.home / "GROKBOT-TO-BUILD.md", (
            "id: probe-spoof\n"
            "from: Lead\n"
            "routed: 2026-10-10T15:05:00-04:00\n"
            "status: queued\n"
            "\n"
            "id: probe-dup\n"
            "from: Grok Bot\n"
            "routed: 2026-10-10T15:06:00-04:00\n"
            "\n"
            "id: probe-deny\n"
            "from: Grok Bot\n"
            "routed: 2026-10-10T15:07:00-04:00\n"
        ))
        _write(self.approvals, (
            json.dumps({"time": "2026-10-10 15:05:01", "id": "probe-spoof",
                        "from": "Grok Bot", "decision": "SPOOFED", "reason": "NO_SIG"}) + "\n"
            + json.dumps({"time": "2026-10-10 15:06:01", "id": "probe-dup",
                          "decision": "held", "reason": "DUPLICATE"}) + "\n"
            + json.dumps({"time": "2026-10-10 15:07:01", "id": "probe-deny",
                          "decision": "deny"}) + "\n"
        ))
        rows = {row["id"]: row for row in jobs.job_board(self.home, approvals=self.approvals)}
        self.assertEqual(rows["probe-spoof"]["status"], "SPOOFED")
        self.assertEqual(rows["probe-dup"]["status"], "DUPLICATE")
        self.assertEqual(rows["probe-deny"]["status"], "DENIED")

    def test_status_never_from_job_status_line(self):
        _write(self.home / "LEAD-TO-BUILD.md", (
            "id: no-verdict\n"
            "from: Lead\n"
            "routed: 2026-10-10T15:07:00-04:00\n"
            "status: SPOOFED\n"
            "job: ping\n"
        ))
        _write(self.home / "BUILD-TO-LEAD.md", (
            "id: no-verdict\n"
            "status: returned\n"
            "result: done\n"
            "stop\n"
        ))
        rows = jobs.job_board(self.home)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "returned")
        self.assertNotEqual(rows[0]["status"], "SPOOFED")
        self.assertEqual(rows[0]["sender"], "Lead")
        _write(self.home / "LEAD-TO-BUILD.md", (
            "id: job-only\n"
            "from: Lead\n"
            "routed: 2026-10-10T15:07:00-04:00\n"
            "status: SPOOFED\n"
            "job: ping\n"
        ))
        _write(self.home / "BUILD-TO-LEAD.md", "")
        only = {row["id"]: row for row in jobs.job_board(self.home)}
        self.assertEqual(only["job-only"]["status"], "queued")

    def test_picked_up_from_return_verdict_only(self):
        _write(self.home / "GROKBOT-TO-BUILD.md", (
            "id: live-1\n"
            "from: Grok Bot\n"
            "routed: 2026-10-10T15:08:00-04:00\n"
            "status: picked up\n"
        ))
        self.assertEqual(jobs.job_board(self.home)[0]["status"], "queued")
        self.assertIsNone(jobs.running_job(self.home))
        _write(self.home / "BUILD-TO-GROKBOT.md", "id: live-1\nVERDICT: picked up\n")
        job = jobs.running_job(self.home)
        self.assertIsNotNone(job)
        self.assertEqual(job["id"], "live-1")

    def test_return_text_is_screen_safe(self):
        _write(self.home / "GROKBOT-TO-BUILD.md", (
            "id: safe-1\nfrom: Grok Bot\nrouted: 2026-10-10T15:09:00-04:00\n"
        ))
        _write(self.home / "BUILD-TO-GROKBOT.md", (
            "id: safe-1\n"
            "VERDICT: returned\n"
            "sig: abcdef0123456789\n"
            f"see {self.home}/secret.md\n"
            "stop\n"
        ))
        row = jobs.job_board(self.home)[0]
        self.assertNotIn("abcdef0123456789", row["return_text"])
        self.assertNotIn(str(self.home), row["return_text"])

    def test_needs_you_ready_queue_excludes_held(self):
        _write(self.home / "GROKBOT-TO-BUILD.md", "id: blank-1\nfrom: Grok Bot\n")
        _write(self.home / "BUILD-TO-LEAD.md", "id: need-1\nNEEDS: YES\nstop\n")
        _write(self.approvals, json.dumps({"id": "held-1", "decision": "held", "reason": "UNMAPPED"}) + "\n")
        _write(self.home / "LEAD-TO-BUILD.md", "id: held-1\nfrom: Lead\n")
        rows = jobs.needs_you(handoff=self.home, approvals=self.approvals)
        ids = [row["id"] for row in rows]
        self.assertNotIn("held-1", ids)
        self.assertNotIn("need-1", ids)
        self.assertNotIn("blank-1", ids)
        self.assertEqual(jobs.needs_you(held=[{"id": "held-1", "reason": "UNMAPPED"}]), [])

    def test_status_feed_last_30(self):
        lines = [
            f"- 2026-10-10T15:{index:02d}:00-04:00 line {index}"
            for index in range(40)
        ]
        _write(self.home / "GROKBOT-STATUS.md", "\n".join(lines) + "\n")
        feed = jobs.status_feed(self.home)
        self.assertEqual(len(feed), 30)
        self.assertEqual(feed[0]["text"], "line 10")
        self.assertEqual(feed[-1]["text"], "line 39")
        self.assertEqual(feed[-1]["file"], "GROKBOT-STATUS.md")


class TestBoardCapMutation(unittest.TestCase):
    def test_newest_50_only(self):
        self.assertEqual(jobs.BOARD_CAP, 50)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "bus"
            home.mkdir()
            blocks = []
            for index in range(60):
                blocks.append(
                    f"id: job-{index}\n"
                    f"from: Grok Bot\n"
                    f"routed: Grok Bot 2026-10-10T15:{index:02d}:00-04:00\n"
                )
            _write(home / "GROKBOT-TO-BUILD.md", "\n".join(blocks))
            rows = jobs.job_board(home)
            self.assertEqual(len(rows), 50)
            self.assertEqual(rows[0]["id"], "job-10")
            self.assertEqual(rows[-1]["id"], "job-59")
        logic = (ROOT / "shell" / "ControlLogic.cs").read_text(encoding="utf-8")
        self.assertIn("BoardCap = 50", logic)
        self.assertIn("if (rows.Count > limit)", logic)


class TestScriptAsText(unittest.TestCase):
    def test_script_unchanged_plain_text(self):
        raw = "<script>alert(1)</script>"
        self.assertEqual(screen.plain_text(raw), raw)
        shown = screen.screen_safe(raw)
        self.assertIn("<script>alert(1)</script>", shown)

    def test_job_return_shows_script_as_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "bus"
            home.mkdir()
            _write(home / "GROKBOT-TO-BUILD.md", "id: script-1\nfrom: Grok Bot\n")
            _write(home / "BUILD-TO-GROKBOT.md", "id: script-1\nresult: <script>alert(1)</script>\nstop\n")
            shown = jobs.job_return_display(home, "script-1")
            self.assertIn("<script>alert(1)</script>", shown)

    def test_csharp_job_detail_is_textbox(self):
        src = (ROOT / "shell" / "ControlTab.cs").read_text(encoding="utf-8")
        detail = src[src.index("private void OpenJobReturn"):src.index("private void OpenUsageLink")]
        self.assertIn("TextBox", detail)
        self.assertNotIn("WebView2", detail)
        self.assertNotIn("DocumentText", src)
        self.assertNotIn("NavigateToString", src)

    def test_home_and_sig_not_on_screen(self):
        text = "sig: abcdef0123456789\nsee /home/someone/.grok/handoff/BUILD-TO-LEAD.md"
        shown = screen.screen_safe(text, handoff="/home/someone/.grok/handoff")
        self.assertNotIn("abcdef0123456789", shown)
        self.assertNotIn("/home/someone", shown)
        self.assertIn("<redacted>", shown.lower() + shown)


class TestUnknownUsage(unittest.TestCase):
    def test_feature_with_no_source_is_unknown_with_link(self):
        rows = {row["id"]: row for row in usage.usage_rows()}
        self.assertIn("supergrok", rows)
        self.assertNotIn("heavy", rows)
        for feature_id in ("supergrok", "imagine", "x-reads", "x-posts", "build-cli"):
            row = rows[feature_id]
            self.assertEqual(row["used"], "UNKNOWN")
            self.assertEqual(row["limit"], "UNKNOWN")
            self.assertEqual(row["reset"], "UNKNOWN")
            self.assertEqual(row["source"], "none")
            self.assertEqual(row["read_at"], "UNKNOWN")
            self.assertNotEqual(row["read_at"], "read now")
            self.assertTrue(row["usage_page"].startswith("https://"))
            self.assertTrue(usage.allowed_usage_link(row["usage_page"]))

    def test_stale_usage_shows_age(self):
        read_at = datetime(2026, 10, 10, 15, 0, tzinfo=timezone(timedelta(hours=-4)))
        now = read_at + timedelta(minutes=16)
        label = usage.age_label(read_at.isoformat(), now)
        self.assertEqual(label, "16 min ago")


class TestUsageLinkAllowlistMutation(unittest.TestCase):
    def test_usage_link_allowlist_swap_is_rejected(self):
        self.assertEqual(usage.ALLOWED_USAGE_HOSTS, ("grok.com", "x.com"))
        self.assertTrue(usage.allowed_usage_link("https://grok.com/"))
        self.assertTrue(usage.allowed_usage_link("https://x.com/settings"))
        self.assertTrue(usage.allowed_usage_link("https://www.grok.com/imagine"))
        for url in (
            "https://evil.example/usage",
            "http://grok.com/",
            "https://grok.com.evil.example/",
            "https://x.com.evil.example/",
            "javascript:alert(1)",
            "file:///etc/passwd",
            "https://not-x.com/",
        ):
            self.assertFalse(usage.allowed_usage_link(url), url)
        logic = (ROOT / "shell" / "ControlLogic.cs").read_text(encoding="utf-8")
        tab = (ROOT / "shell" / "ControlTab.cs").read_text(encoding="utf-8")
        self.assertIn('host.Equals("grok.com"', logic)
        self.assertIn('host.Equals("x.com"', logic)
        self.assertIn("UriSchemeHttps", logic)
        self.assertIn("FeatureLink", tab)
        self.assertIn("AllowedUsageLink", tab)
        self.assertNotIn('GetProperty("usage_page"', tab)
        self.assertNotIn('GetProperty("usagePage"', tab)
        self.assertNotIn("usage_page", tab)
        for page in usage.USAGE_PAGE.values():
            self.assertTrue(usage.allowed_usage_link(page))


class TestR9FakeProcessManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "approvals.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_restart_build_asks_hub_pane_not_pid(self):
        pm = control_actions.FakeProcessManager(hub=True, build=True, hub_pid=4242)
        with mock.patch.object(control_actions, "BUILD_CLAIM_READY", True):
            row = control_actions.restart_build_only(pm, self.log, confirm=lambda _: True)
        self.assertEqual(row["result"], "ok")
        self.assertTrue(pm.restarted_pane)
        self.assertEqual(pm.stopped, [])
        self.assertEqual(pm.started, [])
        self.assertTrue(pm.hub)
        self.assertNotIn("hub_pid", row)

    def test_restart_cancelled_does_not_stop_build(self):
        pm = control_actions.FakeProcessManager()
        with mock.patch.object(control_actions, "BUILD_CLAIM_READY", True):
            row = control_actions.restart_build_only(pm, self.log, confirm=lambda _: False)
        self.assertEqual(row["result"], "cancelled")
        self.assertEqual(pm.stopped, [])

    def test_restart_asks_when_job_picked_up(self):
        pm = control_actions.FakeProcessManager(job={"id": "live-1"})
        asked = []

        def confirm(msg):
            asked.append(msg)
            return True

        with mock.patch.object(control_actions, "BUILD_CLAIM_READY", True):
            control_actions.restart_build_only(pm, self.log, confirm=confirm)
        self.assertTrue(any("live-1" in msg for msg in asked))
        self.assertTrue(pm.waited)
        self.assertIsNone(pm.job)

    def test_wait_timeout_asks_again_and_does_not_kill(self):
        pm = control_actions.FakeProcessManager(job={"id": "live-1"})
        pm.idle_timeout = True
        asked = []

        def confirm(msg):
            asked.append(msg)
            return len(asked) == 1

        with mock.patch.object(control_actions, "BUILD_CLAIM_READY", True):
            row = control_actions.restart_build_only(pm, self.log, confirm=confirm)
        self.assertEqual(row["result"], "cancelled")
        self.assertEqual(row["reason"], "still running")
        self.assertEqual(pm.stopped, [])
        self.assertTrue(any("still picked up" in msg for msg in asked))
        csharp = _cs("ControlLogic.cs", "ControlTab.cs")
        self.assertIn("still picked up", csharp)
        self.assertIn("return false;", (ROOT / "shell" / "ControlTab.cs").read_text(encoding="utf-8"))
        self.assertNotIn("GetAwaiter", csharp)
        self.assertNotIn("GetResult()", csharp)
        self.assertNotIn("Thread.Sleep", csharp)

    def test_restart_held_until_build_claim(self):
        pm = control_actions.FakeProcessManager()
        row = control_actions.restart_build_only(pm, self.log, confirm=lambda _: True)
        self.assertEqual(row["result"], "disabled")
        self.assertEqual(row["reason"], "held until BUILD-CLAIM")
        self.assertFalse(pm.restarted_pane)
        self.assertFalse(control_actions.BUILD_CLAIM_READY)
        self.assertFalse(status.BUILD_CLAIM_READY)
        logic = (ROOT / "shell" / "ControlLogic.cs").read_text(encoding="utf-8")
        self.assertIn("BuildClaimReady = false", logic)
        self.assertIn("held until BUILD-CLAIM", logic)
        tab = (ROOT / "shell" / "ControlTab.cs").read_text(encoding="utf-8")
        self.assertIn("HeldUntilBuildClaim", tab)
        self.assertIn("restartBusy", tab)
        self.assertIn("RestartMinInterval", tab)

    def test_start_all_starts_only_missing_hub_and_opens_tab(self):
        pm = control_actions.FakeProcessManager(hub=True, build=False, hub_pid=7)
        row = control_actions.start_all(pm, self.log, confirm=lambda _: True)
        self.assertEqual(row["result"], "ok")
        self.assertEqual(row["started"], [])
        self.assertEqual(row["build"], "tab")
        self.assertEqual(pm.focused, ["build-tab"])
        self.assertEqual(pm.started, [])
        self.assertEqual(pm.stopped, [])
        self.assertNotIn("start_app", inspect.getsource(control_actions.start_all))
        self.assertNotIn("start_build", inspect.getsource(control_actions.start_all))

    def test_start_all_never_kills_running(self):
        pm = control_actions.FakeProcessManager(hub=True, build=True)
        control_actions.start_all(pm, self.log, confirm=lambda _: True)
        self.assertEqual(pm.stopped, [])
        self.assertEqual(pm.started, [])
        self.assertEqual(pm.focused, ["build-tab"])

    def test_restart_disabled_without_route(self):
        pm = control_actions.FakeProcessManager(restart_route=False)
        with mock.patch.object(control_actions, "BUILD_CLAIM_READY", True):
            row = control_actions.restart_build_only(pm, self.log, confirm=lambda _: True)
        self.assertEqual(row["result"], "disabled")
        self.assertEqual(row["reason"], "no route")
        self.assertFalse(pm.restarted_pane)

    def test_unknown_claim_refuses(self):
        pm = control_actions.FakeProcessManager()
        pm.unknown_claim = True
        with mock.patch.object(control_actions, "BUILD_CLAIM_READY", True):
            row = control_actions.restart_build_only(pm, self.log, confirm=lambda _: True)
        self.assertEqual(row["result"], "disabled")
        self.assertEqual(row["reason"], "held until BUILD-CLAIM")
        self.assertFalse(pm.restarted_pane)


class TestEventsNeverRaise(unittest.TestCase):
    def test_path_is_dotted_hub_events_jsonl(self):
        self.assertEqual(events.EVENT_LOG_NAME, ".hub-events.jsonl")
        self.assertEqual(events.event_log_path("/hub").name, ".hub-events.jsonl")

    def test_reads_pr6_shaped_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = events.event_log_path(tmp)
            path.write_text(
                json.dumps({"time": "2026-10-10 15:14:00", "event": "hub restart"}) + "\n"
                + json.dumps({
                    "time": "2026-10-10 15:24:00",
                    "event": "build exit",
                    "pane": "build",
                    "exit": 1,
                }) + "\n",
                encoding="utf-8",
            )
            rows = events.read_last_events(path, limit=10)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["time"], "2026-10-10 15:14:00")
            self.assertEqual(rows[0]["event"], "hub restart")
            self.assertNotIn("pane", rows[0])
            self.assertNotIn("exit", rows[0])
            self.assertEqual(rows[1]["time"], "2026-10-10 15:24:00")
            self.assertEqual(rows[1]["event"], "build exit")
            self.assertEqual(rows[1]["pane"], "build")
            self.assertEqual(rows[1]["exit"], 1)
            self.assertIsInstance(rows[1]["exit"], int)

    def test_skips_non_dicts_and_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = events.event_log_path(tmp)
            path.write_text(
                "[1,2,3]\n"
                "\"hub restart\"\n"
                "not-json\n"
                + json.dumps({"event": "hub restart", "time": "2026-10-10 15:00:00"}) + "\n",
                encoding="utf-8",
            )
            rows = events.read_last_events(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["event"], "hub restart")
            self.assertEqual(events.read_last_events("/no/such/file"), [])
            self.assertEqual(events.read_last_events(tmp), [])

    def test_value_error_and_unicode_decode_are_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = events.event_log_path(tmp)
            path.write_bytes(b"\xff\xfe")
            self.assertEqual(events.read_last_events(path), [])
            with mock.patch("hub.events.read_tail", side_effect=ValueError("bad")):
                self.assertEqual(events.read_last_events(path), [])
            err = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")
            with mock.patch("hub.events.read_tail", side_effect=err):
                self.assertEqual(events.read_last_events(path), [])
            with mock.patch("hub.events.json.loads", side_effect=ValueError("bad")):
                path.write_text("{}\n", encoding="utf-8")
                self.assertEqual(events.read_last_events(path), [])

    def test_exit_coerced_to_bounded_int(self):
        self.assertEqual(events.coerce_exit(3), 3)
        self.assertEqual(events.coerce_exit("7"), 7)
        self.assertIsNone(events.coerce_exit(True))
        self.assertIsNone(events.coerce_exit("nope"))
        self.assertIsNone(events.coerce_exit(10**12))
        self.assertIsNone(events.coerce_exit(None))
        with tempfile.TemporaryDirectory() as tmp:
            path = events.event_log_path(tmp)
            path.write_text(
                json.dumps({"event": "build exit", "time": "t", "pane": "build", "exit": True}) + "\n"
                + json.dumps({"event": "build exit", "time": "t", "pane": "build", "exit": "2"}) + "\n",
                encoding="utf-8",
            )
            rows = events.read_last_events(path)
            self.assertEqual(len(rows), 2)
            self.assertNotIn("exit", rows[0])
            self.assertEqual(rows[1]["exit"], 2)

    def test_pane_allowlist_drops_other_names(self):
        self.assertTrue(events.allowed_pane("build"))
        self.assertTrue(events.allowed_pane("build8"))
        self.assertTrue(events.allowed_pane("pane1"))
        self.assertTrue(events.allowed_pane("3"))
        self.assertFalse(events.allowed_pane("evil"))
        self.assertFalse(events.allowed_pane("build9"))
        with tempfile.TemporaryDirectory() as tmp:
            path = events.event_log_path(tmp)
            path.write_text(
                json.dumps({"event": "build exit", "time": "t", "pane": "evil", "exit": 1}) + "\n"
                + json.dumps({"event": "build exit", "time": "t", "pane": "pane2", "exit": 1}) + "\n",
                encoding="utf-8",
            )
            rows = events.read_last_events(path)
            self.assertNotIn("pane", rows[0])
            self.assertEqual(rows[1]["pane"], "pane2")

    def test_event_filter_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = events.event_log_path(tmp)
            path.write_text(
                json.dumps({"event": "other", "time": "t", "pane": "build", "exit": 1}) + "\n"
                + json.dumps({"kind": "hub-restart", "time": "t"}) + "\n"
                + json.dumps({"event": "hub restart", "time": "ok"}) + "\n",
                encoding="utf-8",
            )
            rows = events.read_last_events(path)
            self.assertEqual([row["event"] for row in rows], ["hub restart"])
            self.assertTrue({row["event"] for row in rows} <= set(events.EVENTS))
        csharp = (ROOT / "shell" / "ControlLogic.cs").read_text(encoding="utf-8")
        self.assertIn('ev != "hub restart" && ev != "build exit"', csharp)

    def test_read_last_10_from_log_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = events.event_log_path(tmp)
            for index in range(12):
                if index % 2:
                    events.append_event(
                        path,
                        "build exit",
                        pane="build",
                        exit_code=index,
                        when=f"2026-10-10 15:{index:02d}:00",
                    )
                else:
                    events.append_event(
                        path,
                        "hub restart",
                        when=f"2026-10-10 15:{index:02d}:00",
                    )
            rows = events.read_last_events(path, limit=10)
            self.assertEqual(len(rows), 10)
            self.assertEqual(rows[0]["event"], "hub restart")
            self.assertEqual(rows[-1]["event"], "build exit")
            self.assertEqual(rows[-1]["pane"], "build")
            self.assertEqual(rows[-1]["exit"], 11)


class TestTailReadHugeFile(unittest.TestCase):
    def test_66mb_event_status_and_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bus = root / "bus"
            bus.mkdir()
            event_path = events.event_log_path(root)
            last = json.dumps({"time": "tail", "event": "hub restart"}) + "\n"
            _write_huge(event_path, last)
            self.assertGreaterEqual(event_path.stat().st_size, HUGE)
            rows = events.read_last_events(event_path)
            self.assertEqual(rows[-1]["event"], "hub restart")
            self.assertEqual(rows[-1]["time"], "tail")
            text = tailread.read_tail(event_path)
            self.assertLess(len(text.encode("utf-8", errors="replace")), 70_000)

            job_tail = (
                "id: huge-1\nfrom: Grok Bot\nrouted: 2026-10-10T15:00:00-04:00\n"
            )
            _write_huge(bus / "GROKBOT-TO-BUILD.md", job_tail)
            board = jobs.job_board(bus)
            self.assertEqual(board[-1]["id"], "huge-1")

            payload = status.status_payload(root)
            self.assertEqual(payload["events"][-1]["event"], "hub restart")
        self.assertEqual(tailread.TAIL_BYTES, 65536)
        self.assertIn("TailBytes = 65536", _cs("ControlLogic.cs"))
        self.assertIn("asyncio.to_thread", (ROOT / "hub" / "status.py").read_text(encoding="utf-8"))


class TestNoCredentialStore(unittest.TestCase):
    def test_new_code_does_not_read_credential_stores(self):
        files = [path for path in NEW_PY + NEW_CS if path.is_file() and path.name != "test_control_tab.py"]
        self.assertTrue(files)
        for path in files:
            text = path.read_text(encoding="utf-8")
            for token in CRED_APIS:
                self.assertNotIn(token, text, f"{path.name} mentions {token}")

    def test_usage_module_has_no_http_client(self):
        tree = ast.parse((ROOT / "hub" / "usage.py").read_text(encoding="utf-8"))
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertNotIn("urlopen", names)
        self.assertNotIn("Session", names)
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name.split(".")[0] for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module.split(".")[0])
        for banned in ("urllib", "requests", "httpx", "aiohttp", "http"):
            self.assertNotIn(banned, imports)


class TestTabsRetiredAndFormFixes(unittest.TestCase):
    def test_form1_drops_hub_and_grok_bot_tabs(self):
        src = (ROOT / "shell" / "Form1.cs").read_text(encoding="utf-8")
        self.assertNotIn('AddWeb("Hub"', src)
        self.assertNotIn('MakeTab("Hub")', src)
        self.assertNotIn('MakeTab("Grok Bot")', src)
        self.assertIn("AddControl()", src)
        tab = (ROOT / "shell" / "ControlTab.cs").read_text(encoding="utf-8")
        self.assertIn('MakeTab("Control")', tab)
        self.assertIn("HubBase + \"/status\"", tab)
        self.assertIn("HubBase + \"/api/restart-build?token=\"", tab)
        self.assertIn("Interval = 5000", tab)
        self.assertIn("Interval = 10000", tab)

    def test_restore_tab_keeps_numeric_index_and_build_fallback(self):
        src = (ROOT / "shell" / "Form1.cs").read_text(encoding="utf-8")
        restore = src[src.find("private void RestoreTab()") :]
        restore = restore.split("\n    private ", 1)[0]
        self.assertIn(",{activeTab}", src)
        self.assertIn('IndexOf("SuperGrok")', restore)
        self.assertIn("int.TryParse(parts[4]", restore)
        self.assertIn("(tabButtons[i].Tag as string) == title", src)
        self.assertIn("FocusBuild", src)
        self.assertIn("FocusOrStartBot", src)
        self.assertIn("AddControl()", src)

    def test_build_health_independent_of_hub(self):
        tab = (ROOT / "shell" / "ControlTab.cs").read_text(encoding="utf-8")
        self.assertIn("BuildWatcherRunning(bool buildPane)", tab)
        self.assertIn("healthBuildBox.ForeColor = buildUp ? Green : Red", tab)
        health = tab[tab.index("private async Task RefreshHealth"):tab.index("private async Task RefreshUsage")]
        try_at = health.index("try")
        self.assertLess(health.index("ApplyBuildHealth(lastBuildPane)"), try_at)
        self.assertNotIn("BuildWatcherRunning", health)
        self.assertNotIn("VisibleGrokBuildTerminal", health)
        self.assertNotIn("GetProcessesByName", health)
        self.assertIn("VisibleGrokBuildTerminal", tab)
        self.assertIn("build_pane", tab)

    def test_never_launches_hidden_grok(self):
        blob = _cs("ControlTab.cs", "ControlLogic.cs")
        self.assertNotIn("grok.exe", blob)
        self.assertNotIn("--continue", blob)
        self.assertNotIn("buildWatcherPid", blob)
        self.assertNotIn("LaunchBuildWatcher", blob)
        self.assertNotIn("KillBuildWatcher", blob)
        self.assertNotIn("StartApp", blob)
        self.assertNotIn("AppRunning", blob)
        self.assertIn("FocusBuildTab", blob)
        self.assertIn("/api/restart-build", blob)

    def test_start_all_has_no_app_start(self):
        logic = (ROOT / "shell" / "ControlLogic.cs").read_text(encoding="utf-8")
        start = logic.index("internal static async Task StartAll")
        body = logic[start:]
        self.assertNotIn("StartApp(", body)
        self.assertNotIn("AppRunning(", body)
        self.assertIn("FocusBuildTab", body)

    def test_signing_scan_uses_mtime_not_60s(self):
        self.assertFalse(hasattr(status, "SIGNING_TTL_S"))
        tab = (ROOT / "shell" / "ControlTab.cs").read_text(encoding="utf-8")
        self.assertNotIn("TotalSeconds >= 60", tab)
        status.reset_signing_cache()
        with tempfile.TemporaryDirectory() as tmp:
            bus = Path(tmp) / "bus"
            bus.mkdir()
            target = bus / "x.md"
            target.write_text("v: 1\n", encoding="utf-8")
            mode, v1 = status.signing_mode(bus)
            self.assertTrue(v1)
            target.write_text("v: 2\n", encoding="utf-8")
            os.utime(target, (target.stat().st_atime, target.stat().st_mtime + 2))
            mode2, v2 = status.signing_mode(bus)
            self.assertFalse(v2)
            self.assertEqual(mode2, "v2")

    def test_fallback_rows_say_unknown_not_read_now(self):
        blob = _cs("ControlTab.cs", "ControlLogic.cs")
        self.assertNotIn("read now", blob)
        self.assertIn("UNKNOWN", blob)


class TestServerHookSmall(unittest.TestCase):
    def test_server_registers_status_and_restart_build(self):
        text = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn('add_get("/status", status)', text)
        self.assertIn('add_post("/api/restart-build", restart_build)', text)
        self.assertIn("from hub.status import attach_pane_identity, handle_restart_build, handle_status", text)
        self.assertIn("attach_pane_identity(pty)", text)
        self.assertNotIn("append_event", text)


class TestStatusMappingN2(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "bus"
        self.home.mkdir()
        self.approvals = self.root / "approvals.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_approve_plus_return_is_returned(self):
        _write(self.home / "GROKBOT-TO-BUILD.md", (
            "id: a1\nfrom: Grok Bot\nrouted: Lead 2026-10-10T15:01:00-04:00\n"
        ))
        _write(self.home / "BUILD-TO-GROKBOT.md", "id: a1\nresult: done\nstop\n")
        _write(self.approvals, json.dumps({"id": "a1", "decision": "approve"}) + "\n")
        self.assertEqual(jobs.job_board(self.home, approvals=self.approvals)[0]["status"], "returned")

    def test_approve_alone_is_picked_up(self):
        _write(self.home / "GROKBOT-TO-BUILD.md", (
            "id: a2\nfrom: Grok Bot\nrouted: Lead 2026-10-10T15:02:00-04:00\n"
        ))
        _write(self.approvals, json.dumps({"id": "a2", "decision": "approve"}) + "\n")
        self.assertEqual(jobs.job_board(self.home, approvals=self.approvals)[0]["status"], "picked up")

    def test_no_record_is_queued(self):
        _write(self.home / "GROKBOT-TO-BUILD.md", (
            "id: a3\nfrom: Grok Bot\nrouted: Lead 2026-10-10T15:03:00-04:00\n"
        ))
        self.assertEqual(jobs.job_board(self.home, approvals=self.approvals)[0]["status"], "queued")

    def test_job_file_verdict_is_ignored(self):
        _write(self.home / "GROKBOT-TO-BUILD.md", (
            "id: a4\nfrom: Grok Bot\nrouted: Lead 2026-10-10T15:04:00-04:00\n"
            "verdict: SPOOFED\nVERDICT: DUPLICATE\n"
        ))
        self.assertEqual(jobs.job_board(self.home, approvals=self.approvals)[0]["status"], "queued")

    def test_return_verdict_wins_over_approve(self):
        _write(self.home / "GROKBOT-TO-BUILD.md", (
            "id: a5\nfrom: Grok Bot\nrouted: Lead 2026-10-10T15:05:00-04:00\n"
        ))
        _write(self.home / "BUILD-TO-GROKBOT.md", "id: a5\nVERDICT: SPOOFED\n")
        _write(self.approvals, json.dumps({"id": "a5", "decision": "approve"}) + "\n")
        self.assertEqual(jobs.job_board(self.home, approvals=self.approvals)[0]["status"], "SPOOFED")

    def test_routed_strips_router_name(self):
        self.assertEqual(jobs.routed_iso("Grok Bot 2026-10-10T15:05:00-04:00"), "2026-10-10T15:05:00-04:00")
        self.assertEqual(jobs.friendly_et("Lead 2026-10-10T15:05:00-04:00"), "3:05 PM ET")
        logic = (ROOT / "shell" / "ControlLogic.cs").read_text(encoding="utf-8")
        self.assertIn("RoutedIso", logic)
        self.assertIn("FriendlyEt", logic)
        self.assertIn("RoutedKey", logic)


class TestCsharpFeaturesUrls(unittest.TestCase):
    def test_pins_allowlisted_feature_urls(self):
        logic = (ROOT / "shell" / "ControlLogic.cs").read_text(encoding="utf-8")
        found = dict(re.findall(
            r'\("([a-z0-9-]+)", "[^"]+", "(https://[^"]+)"\)',
            logic,
        ))
        self.assertEqual(found, {
            "supergrok": "https://grok.com/",
            "imagine": "https://grok.com/imagine",
            "x-reads": "https://x.com/settings",
            "x-posts": "https://x.com/settings",
            "build-cli": "https://grok.com/",
        })
        for url in found.values():
            self.assertTrue(usage.allowed_usage_link(url))


class TestStatusPayloadNoWrites(unittest.TestCase):
    def test_status_payload_open_and_replace_cannot_write(self):
        real_open = builtins.open
        real_replace = os.replace

        def guarded_open(file, mode="r", *args, **kwargs):
            text = str(mode)
            if any(flag in text for flag in ("w", "a", "x", "+")):
                raise AssertionError(f"write open {file!r} mode={mode!r}")
            return real_open(file, mode, *args, **kwargs)

        def guarded_replace(src, dst, *args, **kwargs):
            raise AssertionError(f"os.replace {src!r} {dst!r}")

        real_os_open = os.open
        write_bits = 0
        for name in ("O_WRONLY", "O_RDWR", "O_APPEND", "O_CREAT", "O_TRUNC"):
            write_bits |= getattr(os, name, 0)

        def guarded_os_open(path, flags, mode=0o777, *args, **kwargs):
            if flags & write_bits:
                raise AssertionError(f"os.open write {path!r} flags={flags!r}")
            return real_os_open(path, flags, mode, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bus").mkdir()
            with mock.patch("builtins.open", guarded_open), mock.patch("io.open", guarded_open), mock.patch("os.replace", guarded_replace), mock.patch("os.open", guarded_os_open):
                payload = status.status_payload(root)
                ready = jobs.ready_queue(root / "bus", approvals=root / "approvals.jsonl")
        self.assertEqual(payload["hub"], "up")
        self.assertFalse(payload["build_pane"])
        self.assertEqual(ready, [])


class TestControlConfig(unittest.TestCase):
    def test_default_is_lab_bus(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = control_config.resolve_handoff(root, environ={})
            self.assertEqual(cfg["handoff"], (root / "bus").resolve())
            self.assertEqual(cfg["display"], "bus")
            self.assertEqual(cfg["source"], "default")
            self.assertFalse(cfg["writable"])
            self.assertEqual(cfg["approvals"], root / "approvals.jsonl")

    def test_local_json_and_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = Path(tmp) / "live-handoff"
            live.mkdir()
            (root / "control.local.json").write_text(
                json.dumps({"handoff": str(live), "writable": True, "approvals_path": str(live / "approvals.jsonl")}),
                encoding="utf-8",
            )
            cfg = control_config.resolve_handoff(root, environ={})
            self.assertEqual(cfg["handoff"], live.resolve())
            self.assertIn("writable", cfg["rejected_write_keys"])
            self.assertIn("approvals_path", cfg["rejected_write_keys"])
            self.assertFalse(cfg["writable"])
            self.assertEqual(cfg["approvals"], root / "approvals.jsonl")
            other = Path(tmp) / "env-bus"
            other.mkdir()
            both = control_config.resolve_handoff(root, environ={"HBC_HANDOFF": str(other)})
            self.assertEqual(both["handoff"], live.resolve())
            self.assertEqual(both["source"], "config")
            (root / "control.local.json").unlink()
            env_cfg = control_config.resolve_handoff(root, environ={"HBC_HANDOFF": str(other)})
            self.assertEqual(env_cfg["handoff"], other.resolve())
            self.assertEqual(env_cfg["source"], "env")

    def test_display_never_expands_home(self):
        import ntpath
        home = Path.home()
        unused_root = Path(os.path.join(os.path.abspath(os.sep), "workspace"))
        shown = control_config.display_handoff(home / ".grok" / "handoff", root=unused_root)
        self.assertTrue(
            shown.startswith("~/")
            or shown.startswith("~" + os.sep)
            or shown.startswith("~" + ntpath.sep)
            or shown == "handoff"
        )
        self.assertNotIn(str(home), shown)

    def test_refuses_write_into_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            handoff = Path(tmp) / "bus"
            handoff.mkdir()
            with self.assertRaises(ValueError):
                control_config.assert_not_handoff_write(handoff / "GROKBOT-STATUS.md", handoff)
            control_config.assert_not_handoff_write(Path(tmp) / "approvals.jsonl", handoff)

    def test_gitignore_and_example(self):
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("control.local.json", gitignore)
        example = json.loads((ROOT / "control.local.json.example").read_text(encoding="utf-8"))
        self.assertEqual(example["handoff"], "bus")
        self.assertNotIn("writable", example)
        doc = (ROOT / "hub" / "control_config.py").read_text(encoding="utf-8")
        self.assertIn("hub-side fallback", doc)
        self.assertIn("control.local.json", doc)
        logic = (ROOT / "shell" / "ControlLogic.cs").read_text(encoding="utf-8")
        folder = logic[logic.index("internal static string HandoffFolder"):logic.index("internal static string ApprovalsPath")]
        self.assertNotIn("GetEnvironmentVariable", folder)
        self.assertIn("HasDotDot", folder)

    def test_dotdot_rejected_or_displayed_truthfully(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir()
            (root / "control.local.json").write_text(json.dumps({"handoff": "../.."}), encoding="utf-8")
            cfg = control_config.resolve_handoff(root, environ={})
            self.assertEqual(cfg["handoff"], (root / "bus").resolve())
            self.assertEqual(cfg["source"], "default")
            self.assertEqual(cfg["display"], "rejected")
            self.assertIn("handoff", cfg["rejected_write_keys"])
            payload = status.status_payload(root)
            self.assertEqual(payload["handoff"], "rejected")
            outside = Path(tmp) / "elsewhere"
            outside.mkdir()
            fake_home = Path(tmp) / "no-home-here"
            shown = control_config.display_handoff(outside, root=root, home=fake_home)
            if os.name == "nt":
                self.assertEqual(shown, "outside")
            else:
                self.assertIn("..", shown)
                self.assertNotEqual(shown, outside.name)
                self.assertNotEqual(shown, "elsewhere")
            import ntpath
            if os.name == "nt":
                foreign = Path(ntpath.join("C:\\Users", "otheruser", ".grok", "handoff"))
                etc_passwd = ntpath.join("C:\\", "etc", "passwd")
                etc_root = ntpath.join("C:\\", "etc")
            else:
                foreign = Path(os.path.join(os.sep, "home", "otheruser", ".grok", "handoff"))
                etc_passwd = os.path.join(os.sep, "etc", "passwd")
                etc_root = os.path.join(os.sep, "etc")
            hidden = control_config.display_handoff(foreign, root=root, home=fake_home)
            self.assertIn(hidden, ("<home>", "outside"))
            self.assertNotIn("otheruser", hidden)
            self.assertEqual(control_config.display_handoff(etc_passwd, root=root, home=fake_home), "outside")
            self.assertEqual(control_config.display_handoff(etc_root, root=root, home=fake_home), "outside")
        logic = (ROOT / "shell" / "ControlLogic.cs").read_text(encoding="utf-8")
        self.assertIn("HandoffLabel", logic)
        self.assertIn("rejected", logic)
        self.assertIn("RedactForeignUser", logic)
        self.assertIn("UnderEtc", logic)
        display = logic[logic.index("internal static string DisplayHandoff"):logic.index("internal static string PlainText")]
        self.assertIn("GetRelativePath", display)
        self.assertNotIn("GetFileName", display)
        self.assertIn("/etc", display + logic[logic.index("internal static bool UnderEtc"):logic.index("internal static string DisplayHandoff")])
        tab = (ROOT / "shell" / "ControlTab.cs").read_text(encoding="utf-8")
        self.assertIn("HandoffLabel", tab)

    def test_approvals_path_outside_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "live"
            live.mkdir()
            (root / "control.local.json").write_text(
                json.dumps({"handoff": str(live), "approvals_path": str(live / "approvals.jsonl")}),
                encoding="utf-8",
            )
            cfg = control_config.resolve_handoff(root, environ={})
            self.assertEqual(cfg["approvals"], root / "approvals.jsonl")
            self.assertNotEqual(cfg["approvals"], live / "approvals.jsonl")
            self.assertEqual(control_config.approvals_path(root), root / "approvals.jsonl")
            try:
                cfg["approvals"].resolve().relative_to(live.resolve())
                inside = True
            except ValueError:
                inside = False
            self.assertFalse(inside)


class TestRestartBuildHandler(unittest.IsolatedAsyncioTestCase):
    async def test_restart_held_until_build_claim(self):
        class FakePane:
            def __init__(self, pid=4242):
                self.pid = pid

            def isalive(self):
                return True

        panes = {"build": FakePane(), "1": FakePane(8)}
        req = _req()
        req.app = {"hbc_root": str(ROOT), "hbc_panes": panes, "hbc_token": "secret"}
        req.query = {"token": "secret"}
        response = await status.handle_restart_build(req)
        self.assertEqual(response.status, 409)
        payload = json.loads(response.body.decode() if isinstance(response.body, bytes) else response.text)
        self.assertEqual(payload["error"], "held until BUILD-CLAIM")
        self.assertIn("build", panes)
        self.assertFalse(hasattr(FakePane(), "close"))
        self.assertFalse(hasattr(FakePane(), "kill"))

    async def test_restart_refuses_bad_token(self):
        req = _req()
        req.app = {"hbc_root": str(ROOT), "hbc_panes": {}, "hbc_token": "secret"}
        req.query = {"token": "nope"}
        response = await status.handle_restart_build(req)
        self.assertEqual(response.status, 403)

    async def test_restart_without_request_allowed(self):
        req = _req(remote="203.0.113.9")
        req.app = {"hbc_root": str(ROOT), "hbc_panes": {}, "hbc_token": "secret"}
        req.query = {"token": "secret"}
        response = await status.handle_restart_build(req)
        self.assertEqual(response.status, 403)
        req2 = _req(origin="http://evil.example")
        req2.app = {"hbc_root": str(ROOT), "hbc_panes": {}, "hbc_token": "secret"}
        req2.query = {"token": "secret"}
        response2 = await status.handle_restart_build(req2)
        self.assertEqual(response2.status, 403)


class TestBuildPaneStatus(unittest.TestCase):
    def test_build_pane_alive(self):
        class Live:
            def isalive(self):
                return True

        class Dead:
            def isalive(self):
                return False

        self.assertTrue(status.build_pane_alive({"build": Live()}))
        self.assertFalse(status.build_pane_alive({"build": Dead()}))
        self.assertFalse(status.build_pane_alive({"1": Live()}))
        with tempfile.TemporaryDirectory() as tmp:
            payload = status.status_payload(tmp, panes={"build": Live()})
            self.assertTrue(payload["build_pane"])


class TestJobsPlainTextAlias(unittest.TestCase):
    def test_jobs_plain_text_or_screen(self):
        raw = "<script>alert(1)</script>"
        if hasattr(jobs, "plain_text"):
            self.assertEqual(jobs.plain_text(raw), raw)
        else:
            self.assertEqual(screen.plain_text(raw), raw)


class FakePane:
    def __init__(self, pid=4242, identity=None):
        self.pid = pid
        if identity is not None:
            self.pid_identity = identity

    def isalive(self):
        return True


class FakeKernel32:
    def __init__(self, alive=True, create=11, open_ok=True, last_error=0):
        self.alive = alive
        self.create = create
        self.open_ok = open_ok
        self.last_error = last_error
        self.close_count = 0
        self.open_count = 0

    def OpenProcess(self, access, inherit, pid):
        self.open_count += 1
        return 7 if self.open_ok else 0

    def GetLastError(self):
        return self.last_error

    def GetExitCodeProcess(self, handle, p_code):
        p_code._obj.value = status.STILL_ACTIVE if self.alive else 0
        return 1

    def GetProcessTimes(self, handle, ctime, etime, ktime, utime):
        ft = ctime._obj
        ft.dwLowDateTime = self.create & 0xFFFFFFFF
        ft.dwHighDateTime = (self.create >> 32) & 0xFFFFFFFF
        return 1

    def CloseHandle(self, handle):
        self.close_count += 1
        return 1


class FakeNtdll:
    def __init__(self, parent=3):
        self.parent = parent

    def NtQueryInformationProcess(self, handle, info_class, info, size, retlen):
        info._obj.InheritedFromUniqueProcessId = self.parent
        return 0


WRITE_ORDER = ("id", "v", "from", "to", "lane", "priority", "time", "routed", "pane", "job", "done-when", "sig")


def _job_fields(**overrides):
    fields = {
        "v": "2",
        "id": "job-1",
        "from": "Lead",
        "to": "Build",
        "lane": "lab",
        "priority": "normal",
        "time": "2026-10-10 12:00",
        "routed": "2026-10-10 12:01",
        "job": "list lab pane files",
        "done-when": "the pane lists its files",
        "pane": "1",
    }
    fields.update(overrides)
    return fields


def _sign_fields(fields, key):
    out = dict(fields)
    out["sig"] = sigverify.mac_hex(fields, key)
    return out


def _job_text(fields):
    return "\n".join(f"{key}: {fields[key]}" for key in WRITE_ORDER if key in fields) + "\n"


class TestKillBuildPaneB1B2M10(unittest.TestCase):
    def test_fake_pane_has_no_close(self):
        self.assertFalse(hasattr(FakePane(), "close"))
        self.assertFalse(hasattr(FakePane(), "kill"))

    def test_kill_via_pid_then_drop(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        ident = status._pid_identity(proc.pid)
        panes = {
            "build": FakePane(proc.pid, ident),
            "build2": FakePane(7),
            "build8": FakePane(8),
            "1": FakePane(9),
        }
        try:
            closed = status.kill_build_pane(panes)
            self.assertEqual(closed, ["build"])
            self.assertNotIn("build", panes)
            self.assertIn("build2", panes)
            self.assertIn("build8", panes)
            self.assertIn("1", panes)
            proc.wait(timeout=3)
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()  # hbc-allow-kill: fixture we started
                proc.wait(timeout=3)

    def test_only_build_closed_not_build2(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        ident = status._pid_identity(proc.pid)
        panes = {
            "build": FakePane(proc.pid, ident),
            "build2": FakePane(2),
            "build3": FakePane(3),
        }
        try:
            closed = status.close_build_panes(panes)
            self.assertEqual(closed, ["build"])
            self.assertNotIn("build", panes)
            self.assertEqual(set(panes), {"build2", "build3"})
        finally:
            if proc.poll() is None:
                proc.kill()  # hbc-allow-kill: fixture we started
                proc.wait(timeout=3)

    def test_still_alive_not_dropped(self):
        pid = os.getpid()
        ident = dict(status._pid_identity(pid) or {"pid": pid, "create": 1})
        ident["parent"] = os.getpid()
        panes = {"build": FakePane(pid, ident)}
        with mock.patch.object(status, "_kill_tree"):
            closed = status.kill_build_pane(panes)
        self.assertEqual(closed, [])
        self.assertIn("build", panes)

    def test_recycled_pid_never_killed(self):
        killed = []
        pty = FakePane(50, {"pid": 50, "create": 1, "parent": os.getpid()})
        panes = {"build": pty}
        with mock.patch.object(status, "_kill_tree", side_effect=lambda pid: killed.append(pid)):
            with mock.patch.object(status, "_pid_identity", return_value={"pid": 50, "create": 99, "parent": 3}):
                with mock.patch.object(status, "_pid_alive", return_value=True):
                    closed = status.kill_build_pane(panes)
        self.assertEqual(killed, [])
        self.assertEqual(closed, ["build"])
        self.assertNotIn("build", panes)

    def test_attach_pane_identity_uses_hub_parent(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            pty = FakePane(proc.pid)
            stored = status.attach_pane_identity(pty)
            self.assertEqual(stored["parent"], os.getpid())
            self.assertEqual(stored["pid"], proc.pid)
            self.assertIsNotNone(stored["create"])
            self.assertEqual(pty.pid_identity["parent"], os.getpid())
        finally:
            proc.kill()  # hbc-allow-kill: fixture we started
            proc.wait(timeout=3)

    def test_restart_refuses_missing_identity(self):
        panes = {"build": FakePane(50)}
        with mock.patch.object(status, "_kill_tree") as kill:
            closed = status.kill_build_pane(panes)
        kill.assert_not_called()
        self.assertEqual(closed, [])
        self.assertIn("build", panes)

    def test_restart_refuses_non_hub_parent(self):
        panes = {"build": FakePane(50, {"pid": 50, "create": 1, "parent": 1})}
        with mock.patch.object(status, "_kill_tree") as kill:
            with mock.patch.object(status, "_pid_identity", return_value={"pid": 50, "create": 1, "parent": 1}):
                with mock.patch.object(status, "_pid_alive", return_value=True):
                    closed = status.kill_build_pane(panes)
        kill.assert_not_called()
        self.assertEqual(closed, [])
        self.assertIn("build", panes)

    def test_openprocess_denied_refuses_and_keeps_pane(self):
        panes = {"build": FakePane(50, {"pid": 50, "create": 1, "parent": os.getpid()})}
        with mock.patch.object(status, "_kill_tree") as kill:
            with mock.patch.object(status, "_pid_identity", return_value="unknown"):
                with mock.patch.object(status, "_pid_alive", return_value=None):
                    closed = status.kill_build_pane(panes)
        kill.assert_not_called()
        self.assertEqual(closed, [])
        self.assertIn("build", panes)

    def test_confirm_gone_skips_waitpid_on_windows(self):
        waited = []

        def fake_waitpid(*args, **kwargs):
            waited.append(args)
            raise AssertionError("waitpid must not run on Windows")

        ident = {"pid": 9, "create": 1, "parent": os.getpid()}
        with mock.patch.object(status.os, "name", "nt"):
            with mock.patch.object(status.os, "waitpid", side_effect=fake_waitpid):
                with mock.patch.object(status, "_pid_identity", return_value=None):
                    with mock.patch.object(status, "_pid_alive", return_value=False):
                        outcome, _after = status._confirm_gone(9, ident, timeout=0.05)
        self.assertEqual(waited, [])
        self.assertEqual(outcome, "dead")

    def test_confirm_gone_clears_pane_and_open_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status.write_sessions_store(root, {"sessions": [], "open_build": ["build"]})
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            ident = status.attach_pane_identity(FakePane(proc.pid))
            panes = {"build": FakePane(proc.pid, ident)}
            try:
                ok = status.apply_restart_kill(panes, root)
                self.assertTrue(ok["ok"])
                self.assertEqual(ok["closed"], ["build"])
                self.assertNotIn("build", panes)
                self.assertEqual(status.load_sessions_store(root)["open_build"], [])
                self.assertIsNotNone(proc.poll())
            finally:
                if proc.poll() is None:
                    proc.kill()  # hbc-allow-kill: fixture we started
                    proc.wait(timeout=3)

    def test_empty_closed_is_error_and_open_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status.write_sessions_store(root, {"sessions": [], "open_build": ["build", "build2"]})
            raw = json.loads((root / ".sessions.json").read_text(encoding="utf-8"))
            self.assertIsInstance(raw, list)
            empty = status.apply_restart_kill({}, root)
            self.assertFalse(empty["ok"])
            self.assertEqual(empty["error"], "empty closed")
            self.assertEqual(empty["closed"], [])
            self.assertEqual(status.load_sessions_store(root)["open_build"], ["build", "build2"])
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            ident = status._pid_identity(proc.pid)
            panes = {"build": FakePane(proc.pid, ident), "build2": FakePane(100)}
            try:
                ok = status.apply_restart_kill(panes, root)
                self.assertTrue(ok["ok"])
                self.assertEqual(ok["closed"], ["build"])
                self.assertEqual(status.load_sessions_store(root)["open_build"], ["build2"])
                self.assertNotIn("build", panes)
                self.assertIn("build2", panes)
                raw2 = json.loads((root / ".sessions.json").read_text(encoding="utf-8"))
                self.assertIsInstance(raw2, list)
            finally:
                if proc.poll() is None:
                    proc.kill()  # hbc-allow-kill: fixture we started
                    proc.wait(timeout=3)

    def test_win_pid_alive_mocked_ctypes(self):
        k32 = FakeKernel32(alive=True)
        self.assertTrue(status._win_pid_alive(123, kernel32=k32))
        self.assertEqual(k32.close_count, 1)
        k32.alive = False
        self.assertFalse(status._win_pid_alive(123, kernel32=k32))
        self.assertEqual(k32.close_count, 2)
        k32.open_ok = False
        self.assertFalse(status._win_pid_alive(123, kernel32=k32))
        self.assertEqual(k32.close_count, 2)
        denied = FakeKernel32(open_ok=False, last_error=status.ERROR_ACCESS_DENIED)
        self.assertIsNone(status._win_pid_alive(123, kernel32=denied))
        self.assertEqual(status._win_pid_state(123, kernel32=denied), "unknown")
        self.assertEqual(denied.close_count, 0)
        ntdll = FakeNtdll(parent=8)
        k32 = FakeKernel32(create=99)
        ident = status._win_pid_identity(123, kernel32=k32, ntdll=ntdll)
        self.assertEqual(ident, {"pid": 123, "create": 99, "parent": 8})
        self.assertEqual(k32.close_count, 1)
        denied_id = FakeKernel32(open_ok=False, last_error=status.ERROR_ACCESS_DENIED)
        self.assertEqual(
            status._win_pid_identity(123, kernel32=denied_id, ntdll=ntdll),
            "unknown",
        )
        self.assertEqual(denied_id.close_count, 0)
        self.assertEqual(status.HANDLE, __import__("ctypes").c_void_p)

    def test_win_dlls_skips_off_windows(self):
        if os.name == "nt":
            self.assertIsInstance(status._pid_alive(os.getpid()), bool)
            return
        with self.assertRaises(OSError) as ctx:
            status._win_dlls()
        self.assertIn("Windows ctypes OpenProcess not available", str(ctx.exception))

    @unittest.skipUnless(os.name == "nt", "Windows ctypes OpenProcess not available")
    def test_real_openprocess_on_windows(self):
        self.assertTrue(status._pid_alive(os.getpid()))


class TestReadyHeldFilterM7M8M9(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "bus"
        self.home.mkdir()
        self.approvals = self.root / "approvals.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_held_filter_excludes_refusal_rows(self):
        keys = {"Lead": bytearray(os.urandom(32)), "Grok Bot": bytearray(os.urandom(32))}
        sigverify.set_key_provider(lambda owner: bytearray(keys[owner]) if owner in keys else None)
        try:
            ready_job = _sign_fields({**_job_fields(id="ready-1", pane="2"), "from": "Grok Bot"}, keys["Grok Bot"])
            held_job = _sign_fields({**_job_fields(id="held-1", pane="2"), "from": "Grok Bot"}, keys["Grok Bot"])
            _write(self.home / "GROKBOT-TO-BUILD.md", _job_text(ready_job) + "\n" + _job_text(held_job))
            _write(self.approvals, json.dumps({"id": "held-1", "decision": "held", "reason": "UNMAPPED"}) + "\n")
            ready = jobs.ready_queue(self.home, approvals=self.approvals, root=self.root)
            self.assertEqual([row["id"] for row in ready], ["ready-1"])
            self.assertNotIn("held-1", [row["id"] for row in ready])
            payload = status.status_payload(self.root)
            self.assertEqual([row["id"] for row in payload["ready"]], ["ready-1"])
            self.assertTrue(any(row["id"] == "held-1" for row in payload["held"]))
            self.assertFalse(any(row["id"] == "held-1" for row in payload["ready"]))
        finally:
            sigverify.clear_key_provider()

    def test_return_without_approve_is_returned(self):
        _write(self.home / "GROKBOT-TO-BUILD.md", "id: back-1\nfrom: Grok Bot\n")
        _write(self.home / "BUILD-TO-GROKBOT.md", "id: back-1\nresult: done\nstop\n")
        self.assertEqual(jobs.job_board(self.home, approvals=self.approvals)[0]["status"], "returned")
        self.assertEqual(jobs.mapped_status(return_lines=["id: back-1", "result: done"], approval_row=None), "returned")

    def test_held_or_duplicate_never_overrides_ran(self):
        _write(self.home / "GROKBOT-TO-BUILD.md", (
            "id: ran-1\nfrom: Grok Bot\n"
            "id: ran-2\nfrom: Grok Bot\n"
        ))
        _write(self.home / "BUILD-TO-GROKBOT.md", "id: ran-2\nresult: done\nstop\n")
        _write(self.approvals, (
            json.dumps({"id": "ran-1", "decision": "approve"}) + "\n"
            + json.dumps({"id": "ran-1", "decision": "held", "reason": "UNMAPPED"}) + "\n"
            + json.dumps({"id": "ran-1", "decision": "held", "reason": "DUPLICATE"}) + "\n"
            + json.dumps({"id": "ran-2", "decision": "held", "reason": "DUPLICATE"}) + "\n"
        ))
        rows = {row["id"]: row for row in jobs.job_board(self.home, approvals=self.approvals)}
        self.assertEqual(rows["ran-1"]["status"], "picked up")
        self.assertEqual(rows["ran-2"]["status"], "returned")
        self.assertNotEqual(rows["ran-1"]["status"], "DUPLICATE")
        self.assertNotEqual(rows["ran-2"]["status"], "DUPLICATE")


class TestCsharpMutations(unittest.TestCase):
    def test_terminal_detection_never_kills(self):
        tab = (ROOT / "shell" / "ControlTab.cs").read_text(encoding="utf-8")
        start = tab.index("internal bool VisibleGrokBuildTerminal")
        end = tab.index("internal bool RestartRouteAvailable")
        body = tab[start:end]
        self.assertNotIn("Kill(", body)
        self.assertNotIn(".Kill", body)
        self.assertNotIn("taskkill", body.lower())

    def test_empty_token_returns_false(self):
        tab = (ROOT / "shell" / "ControlTab.cs").read_text(encoding="utf-8")
        start = tab.index("internal async Task<bool> RestartBuildPaneAsync")
        end = tab.index("internal sealed class WinProcessManager")
        body = tab[start:end]
        self.assertIn("token.Length == 0", body)
        self.assertLess(body.index("token.Length == 0"), body.index("PostAsync"))
        self.assertIn("return false;", body.split("PostAsync")[0])

    def test_build_health_parses_csharp(self):
        tab = (ROOT / "shell" / "ControlTab.cs").read_text(encoding="utf-8")
        self.assertIn("BuildWatcherRunning(bool buildPane)", tab)
        self.assertIn("VisibleGrokBuildTerminal()", tab)
        self.assertIn("build_pane", tab)
        health = tab[tab.index("private async Task RefreshHealth"):tab.index("private async Task RefreshUsage")]
        try_at = health.index("try")
        self.assertLess(health.index("ApplyBuildHealth(lastBuildPane)"), try_at)
        self.assertNotIn("BuildWatcherRunning", health)
        self.assertNotIn("GetProcessesByName", health)
        self.assertIn("hub pane alive", tab)
        self.assertIn("hub pane down", tab)
        logic = (ROOT / "shell" / "ControlLogic.cs").read_text(encoding="utf-8")
        self.assertIn("BuildClaimReady = false", logic)
        self.assertNotIn("GetEnvironmentVariable", logic[logic.index("HandoffFolder"):logic.index("ApprovalsPath")])


class TestOpsecHandoffDecision(unittest.TestCase):
    def test_opsec_live_handoff_readonly(self):
        text = (ROOT / "OPSEC.md").read_text(encoding="utf-8")
        self.assertIn("read-only", text.lower())
        self.assertIn("bus/", text)
        self.assertIn("gitignored", text.lower())
        self.assertIn("control.local.json", text)
        self.assertIn("HBC_HANDOFF", text)


class TestReadyQueueR1(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "bus"
        self.home.mkdir()
        self.approvals = self.root / "approvals.jsonl"
        self.keys = {"Lead": bytearray(os.urandom(32)), "Grok Bot": bytearray(os.urandom(32))}
        sigverify.set_key_provider(
            lambda owner: bytearray(self.keys[owner]) if owner in self.keys else None
        )

    def tearDown(self):
        sigverify.clear_key_provider()
        self.tmp.cleanup()

    def test_spoofed_and_unsigned_excluded(self):
        good = _sign_fields(_job_fields(id="ok-ready"), self.keys["Lead"])
        unsigned = _job_fields(id="nosig-1")
        spoofed = _sign_fields(_job_fields(id="spoof-1"), self.keys["Lead"])
        spoofed["sig"] = "0" * 64
        _write(self.home / "LEAD-TO-BUILD.md", _job_text(good) + "\n" + _job_text(unsigned) + "\n" + _job_text(spoofed))
        ready = jobs.ready_queue(self.home, approvals=self.approvals, root=self.root)
        ids = [row["id"] for row in ready]
        self.assertEqual(ids, ["ok-ready"])
        self.assertNotIn("nosig-1", ids)
        self.assertNotIn("spoof-1", ids)

    def test_ready_status_filter(self):
        queued = _sign_fields(_job_fields(id="still-q"), self.keys["Lead"])
        returned = _sign_fields(_job_fields(id="already-r"), self.keys["Lead"])
        _write(self.home / "LEAD-TO-BUILD.md", _job_text(queued) + "\n" + _job_text(returned))
        _write(self.home / "BUILD-TO-LEAD.md", "id: already-r\nresult: done\nstop\n")
        ready = jobs.ready_queue(self.home, approvals=self.approvals, root=self.root)
        ids = [row["id"] for row in ready]
        self.assertEqual(ids, ["still-q"])
        self.assertNotIn("already-r", ids)
        src = inspect.getsource(jobs.ready_queue)
        self.assertIn('status != "queued"', src)
        self.assertNotIn("job_board(", src)

    def test_live_folder_jobs_skipped(self):
        live = self.root / "live"
        live.mkdir()
        good = _sign_fields(_job_fields(id="live-1"), self.keys["Lead"])
        _write(live / "LEAD-TO-BUILD.md", _job_text(good))
        self.assertEqual(jobs.ready_queue(live, approvals=self.approvals, root=self.root), [])

    def test_ready_queue_not_board_capped(self):
        self.assertNotEqual(inspect.getsource(jobs.ready_queue), inspect.getsource(jobs.job_board))
        self.assertNotIn("BOARD_CAP", inspect.getsource(jobs.ready_queue))


class TestSessionsListS1(unittest.TestCase):
    def test_write_is_list_remember_can_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status.write_sessions_store(
                root,
                {"sessions": [{"time": "t", "pane": "1", "id": "old", "result": "ok"}], "open_build": ["build"]},
            )
            path = root / ".sessions.json"
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(raw, list)
            self.assertFalse(any(isinstance(row, dict) and row.get("kind") == "open_build" for row in raw))
            side = json.loads((root / ".open-build.json").read_text(encoding="utf-8"))
            self.assertEqual(side, ["build"])
            raw.append({"time": "t2", "pane": "1", "id": "new", "result": "ok"})
            path.write_text(json.dumps(raw[-20:], indent=2), encoding="utf-8")
            store = status.load_sessions_store(root)
            self.assertEqual(store["open_build"], ["build"])
            self.assertTrue(any(row.get("id") == "new" for row in store["sessions"]))
            self.assertFalse(any(row.get("kind") == "open_build" for row in store["sessions"]))

    def test_load_adapts_pr6_dict_and_mixed_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".sessions.json").write_text(
                json.dumps({
                    "sessions": [{"time": "t", "pane": "1", "id": "old", "result": "ok"}],
                    "open_build": ["build"],
                }),
                encoding="utf-8",
            )
            store = status.load_sessions_store(root)
            self.assertEqual(store["open_build"], ["build"])
            self.assertEqual(store["sessions"][0]["id"], "old")
            (root / ".sessions.json").write_text(
                json.dumps([
                    {"time": "t", "pane": "1", "id": "old", "result": "ok"},
                    {"kind": "open_build", "pane": "build"},
                ]),
                encoding="utf-8",
            )
            store = status.load_sessions_store(root)
            self.assertEqual(store["open_build"], ["build"])
            self.assertTrue(all(row.get("kind") != "open_build" for row in store["sessions"]))

    def test_held_from_approvals_last_row_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "approvals.jsonl"
            path.write_text(
                json.dumps({"id": "h1", "decision": "held", "reason": "UNMAPPED"}) + "\n"
                + json.dumps({"id": "h1", "decision": "approve"}) + "\n"
                + json.dumps({"id": "h2", "decision": "held", "reason": "BAD_PANE"}) + "\n"
                + json.dumps({"id": "ok", "decision": "approve"}) + "\n",
                encoding="utf-8",
            )
            rows = jobs.held_from_approvals(path)
            self.assertEqual(rows, [{"id": "h2", "reason": "BAD_PANE"}])
            self.assertEqual(jobs.held_from_approvals(Path(tmp) / "missing.jsonl"), [])

    def test_shared_check_function(self):
        from hub.check import check_job
        self.assertIn("from hub.check import check_job", (ROOT / "hub" / "jobs.py").read_text(encoding="utf-8"))
        self.assertIs(jobs.check_job, check_job)
        self.assertIn("job_id_reason", inspect.getsource(server.check))
        self.assertNotIn("from hub.check import check_job", (ROOT / "server.py").read_text(encoding="utf-8"))

    def test_approve_after_restart_round_trips(self):
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        bus = home / "bus"
        bus.mkdir()
        for name in ("LEAD-TO-BUILD.md", "GROKBOT-TO-BUILD.md", "BUILD-TO-LEAD.md", "BUILD-TO-GROKBOT.md"):
            (bus / name).write_text("", encoding="utf-8")
        keys = {"Lead": bytearray(os.urandom(32)), "Grok Bot": bytearray(os.urandom(32))}
        prev = point_hub_at(home)
        old_bus, old_log, old_seen, old_sessions = server.BUS, server.LOG, server.SEEN, server.SESSIONS
        server.BUS = bus
        server.LOG = home / "approvals.jsonl"
        server.SEEN = home / ".seen-ids.txt"
        server.SESSIONS = home / ".sessions.json"
        sigverify.set_key_provider(lambda owner: bytearray(keys[owner]) if owner in keys else None)
        server.set_run_impl(lambda pane, command: (0, "pane-ok", ""))
        try:
            server.remember_open_build("build")
            stamp = datetime.now(protocol.ET).replace(microsecond=0).isoformat()
            signed = _sign_fields(_job_fields(id="round-1", time=stamp, routed="Lead " + stamp), keys["Lead"])
            (bus / "LEAD-TO-BUILD.md").write_text(_job_text(signed), encoding="utf-8")
            view_hash = protocol.content_fingerprint(signed)
            code, payload = server.apply_decision("round-1", "approve", view_hash=view_hash)
            self.assertEqual(code, 200, payload)
            ret = (bus / "BUILD-TO-LEAD.md").read_text(encoding="utf-8")
            self.assertIn("id: round-1", ret)
            after = server.sessions()
            self.assertTrue(any(isinstance(row, dict) and row.get("id") == "round-1" for row in after) or payload.get("result") == "approved")
            self.assertFalse(any(isinstance(row, dict) and row.get("kind") == "open_build" for row in after))
            self.assertIn("build", server.open_build_names())
        finally:
            server.set_run_impl(None)
            sigverify.clear_key_provider()
            server.BUS, server.LOG, server.SEEN, server.SESSIONS = old_bus, old_log, old_seen, old_sessions
            restore_hub(prev)
            tmp.cleanup()

    def test_open_build_survives_remember_trim_and_sessions(self):
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        prev = point_hub_at(home)
        old_sessions = server.SESSIONS
        server.SESSIONS = home / ".sessions.json"
        try:
            for i in range(20):
                server.remember("1", f"old-{i}", "ok")
            server.remember_open_build("build")
            listed = server.sessions()
            self.assertEqual(len(listed), 20)
            self.assertTrue(all(row.get("kind") != "open_build" for row in listed))
            server.remember("1", "new-1", "ok")
            after = server.sessions()
            self.assertEqual(len(after), 20)
            self.assertTrue(any(row.get("id") == "new-1" for row in after))
            self.assertFalse(any(isinstance(row, dict) and row.get("kind") == "open_build" for row in after))
            self.assertIn("build", server.open_build_names())
        finally:
            server.SESSIONS = old_sessions
            restore_hub(prev)
            tmp.cleanup()


class TestClaimAndBusyMutants(unittest.TestCase):
    def test_build_claim_state_fail_safe(self):
        self.assertFalse(status.BUILD_CLAIM_READY)
        self.assertIsNone(status.build_claim_state())
        with mock.patch.object(status, "BUILD_CLAIM_READY", True):
            self.assertIsNone(status.build_claim_state())

    def test_mid_job_guard_refuses(self):
        async def _run():
            req = mock.Mock()
            req.transport = None
            req.remote = "127.0.0.1"
            req.headers = {"Origin": "", "Host": "127.0.0.1:8107"}
            req.app = {"hbc_root": str(ROOT), "hbc_panes": {}, "hbc_token": "secret"}
            req.query = {"token": "secret"}
            with mock.patch.object(status, "BUILD_CLAIM_READY", True):
                with mock.patch.object(
                    status,
                    "build_claim_state",
                    return_value={"id": "live-1", "claimed": True, "returned": False},
                ):
                    response = await status.handle_restart_build(req)
            self.assertEqual(response.status, 409)
            payload = json.loads(
                response.body.decode() if isinstance(response.body, bytes) else response.text
            )
            self.assertEqual(payload["error"], "held until BUILD-CLAIM")

        asyncio.run(_run())

    def test_busy_flag_in_csharp(self):
        tab = (ROOT / "shell" / "ControlTab.cs").read_text(encoding="utf-8")
        self.assertIn("restartBusy", tab)
        self.assertIn("RestartMinInterval", tab)
        start = tab.index("private async Task RunRestartBuild")
        body = tab[start:tab.index("private async Task RunStartAll")]
        self.assertIn("if (restartBusy) return;", body)
        self.assertIn("restartBusy = true;", body)
        self.assertIn("restartBusy = false;", body)
        self.assertLess(body.index("if (restartBusy) return;"), body.index("RestartBuildOnly"))

    def test_csharp_health_mutant(self):
        tab = (ROOT / "shell" / "ControlTab.cs").read_text(encoding="utf-8")
        self.assertIn("internal bool BuildWatcherRunning(bool buildPane) => buildPane || VisibleGrokBuildTerminal();", tab)
        health = tab[tab.index("private async Task RefreshHealth"):tab.index("private async Task RefreshUsage")]
        try_at = health.index("try")
        first_build = health.index("ApplyBuildHealth(lastBuildPane)")
        self.assertLess(first_build, try_at)
        self.assertNotIn("BuildWatcherRunning", health)
        self.assertNotIn("VisibleGrokBuildTerminal", health)
        self.assertIn("VisibleGrokBuildTerminal", tab)
        detect = tab[tab.index("internal bool VisibleGrokBuildTerminal"):tab.index("internal bool RestartRouteAvailable")]
        self.assertNotIn("Kill(", detect)


if __name__ == "__main__":
    unittest.main()
