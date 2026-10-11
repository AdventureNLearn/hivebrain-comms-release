"""HBC_HUB_PORT stays 127.0.0.1-only. Default is the named constant."""

import asyncio
import hashlib
import io
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server
from tests.live_guard import install_live_state_guard

install_live_state_guard()

RUNTIME_GLOBS = (
    "server.py",
    "build.html",
    "desk.js",
    "Start-HiveBrainComms.ps1",
    "Focus-Or-Start-Shell.ps1",
    "tools/Claim-BusJob.ps1",
    "tools/Approve-BusJob.ps1",
)
ALLOWED_8107 = re.compile(
    r"(DEFAULT_HUB_PORT|DefaultHubPort|\$DefaultHubPort)\s*=\s*8107\b"
)
BUILD_HTML_506EEBD = ROOT / "tests" / "fixtures" / "build.html.506eebd"
BUILD_HTML_506EEBD_SHA256 = (
    "8dccfc1ae67f5a0df08f7abc5f07496e9fd33d5d0c2e4d0f5e5fb8d2c588f672"
)
BUILD_HTML_13E904B = ROOT / "tests" / "fixtures" / "build.html.13e904b"
BUILD_HTML_13E904B_SHA256 = (
    "daa5d622c60e10b1c33e96cf20e7f9cf010267862202a420ed7651f87300c0fe"
)
LOOPBACK_HUB_RE = r"^http://127\.0\.0\.1(:\d+)?$"


def _bytes_lf(path):
    return path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")


class TestHubPort(unittest.TestCase):
    def setUp(self):
        self.prev_port = server.PORT
        self.prev_host = server.HUB_HTTP_HOST

    def tearDown(self):
        server.PORT = self.prev_port
        server.HUB_HTTP_HOST = self.prev_host

    def test_parse_default_and_8117(self):
        port, warning = server.parse_hub_port("")
        self.assertEqual(port, server.DEFAULT_HUB_PORT)
        self.assertIsNone(warning)
        port, warning = server.parse_hub_port(None)
        self.assertEqual(port, server.DEFAULT_HUB_PORT)
        self.assertIsNone(warning)
        port, warning = server.parse_hub_port("8117")
        self.assertEqual(port, 8117)
        self.assertIsNone(warning)
        port, warning = server.parse_hub_port(" 8117 ")
        self.assertEqual(port, 8117)
        self.assertIsNone(warning)

    def test_parse_refusal_uses_default(self):
        for raw in ("80", "1023", "65536", "-1", "+8117", "foo", "8107.0", "+8117.1", "0"):
            port, warning = server.parse_hub_port(raw)
            self.assertEqual(port, server.DEFAULT_HUB_PORT, raw)
            self.assertIsNotNone(warning, raw)
            self.assertIn("refused", warning, raw)

    def test_apply_logs_refusal(self):
        logs = io.StringIO()
        with patch("sys.stdout", logs):
            port = server.apply_hub_port("nope")
        self.assertEqual(port, server.DEFAULT_HUB_PORT)
        self.assertIn("hub-port:", logs.getvalue())
        self.assertIn("refused", logs.getvalue())

    def test_host_check_8117_refuses_8107(self):
        server.apply_hub_port("8117")
        self.assertEqual(server.PORT, 8117)
        self.assertEqual(server.HUB_HTTP_HOST, "127.0.0.1:8117")
        self.assertTrue(server.hub_host_ok("127.0.0.1:8117"))
        self.assertFalse(server.hub_host_ok("127.0.0.1:8107"))
        self.assertFalse(server.hub_host_ok("localhost:8117"))
        self.assertTrue(server.claim_host_ok("127.0.0.1:8117"))
        self.assertFalse(server.claim_host_ok("localhost:8117"))
        self.assertFalse(server.claim_host_ok("[::1]:8117"))
        self.assertFalse(server.claim_host_ok("127.0.0.1:8107"))
        self.assertFalse(server.claim_host_ok("localhost:8107"))
        self.assertFalse(server.claim_host_ok("evil.example:8117"))

        async def run():
            app = server.make_app()
            async with TestClient(TestServer(app)) as client:
                ok = await client.get("/", headers={"Host": "127.0.0.1:8117"})
                old = await client.get("/", headers={"Host": "127.0.0.1:8107"})
                return ok.status, old.status

        ok_status, old_status = asyncio.run(run())
        self.assertEqual(ok_status, 200)
        self.assertEqual(old_status, 421)

    def test_claim_host_ok_tracks_port(self):
        server.apply_hub_port(str(server.DEFAULT_HUB_PORT))
        self.assertTrue(server.claim_host_ok("127.0.0.1:%s" % server.DEFAULT_HUB_PORT))
        server.apply_hub_port("8117")
        self.assertTrue(server.claim_host_ok("127.0.0.1:8117"))
        self.assertFalse(server.claim_host_ok("127.0.0.1:%s" % server.DEFAULT_HUB_PORT))

    def test_no_hardcoded_8107_outside_default_constant(self):
        hits = []
        paths = [ROOT / name for name in RUNTIME_GLOBS]
        paths.extend(sorted((ROOT / "shell").glob("*.cs")))
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for index, line in enumerate(text.splitlines(), 1):
                if "8107" not in line:
                    continue
                if ALLOWED_8107.search(line):
                    continue
                hits.append("%s:%s:%s" % (path.relative_to(ROOT), index, line.strip()))
        self.assertEqual(hits, [], "hardcoded 8107 remains: %s" % "; ".join(hits))

    def test_shell_and_scripts_use_env_port(self):
        form = (ROOT / "shell" / "Form1.cs").read_text(encoding="utf-8")
        self.assertIn("HubBase", form)
        self.assertIn("HBC_HUB_PORT", form)
        self.assertIn("ReadHubPort()", form)
        self.assertNotIn("http://127.0.0.1:8107", form)
        self.assertIn('host == "127.0.0.1" && target.Port == new Uri(HubBase).Port', form)
        start = (ROOT / "Start-HiveBrainComms.ps1").read_text(encoding="utf-8")
        self.assertIn("HBC_HUB_PORT", start)
        self.assertIn("$Url = \"http://127.0.0.1:$HubPort/\"", start)
        claim = (ROOT / "tools" / "Claim-BusJob.ps1").read_text(encoding="utf-8")
        self.assertIn("Get-HubBase", claim)
        self.assertIn("Test-LoopbackHub", claim)
        self.assertIn(LOOPBACK_HUB_RE, claim)
        self.assertNotIn("localhost", claim.split("function Test-LoopbackHub", 1)[1].split("function ", 1)[0])
        approve = (ROOT / "tools" / "Approve-BusJob.ps1").read_text(encoding="utf-8")
        self.assertIn("Get-HubBase", approve)
        self.assertIn("Test-LoopbackHub", approve)
        self.assertIn("HBC_HUB_PORT", approve)
        self.assertIn(LOOPBACK_HUB_RE, approve)
        self.assertNotIn("localhost", approve.split("function Test-LoopbackHub", 1)[1].split("function ", 1)[0])
        build = (ROOT / "build.html").read_text(encoding="utf-8")
        self.assertIn("location.host", build)
        desk = (ROOT / "desk.js").read_text(encoding="utf-8")
        self.assertIn("location.host", desk)

    def test_parse_refuses_fullwidth_digits(self):
        raw = "\uff18\uff11\uff11\uff17"  # full-width 8117
        port, warning = server.parse_hub_port(raw)
        self.assertEqual(port, server.DEFAULT_HUB_PORT)
        self.assertIsNotNone(warning)
        self.assertIn("refused", warning)
        mixed = "81\uff117"
        port, warning = server.parse_hub_port(mixed)
        self.assertEqual(port, server.DEFAULT_HUB_PORT)
        self.assertIsNotNone(warning)

    def test_parse_is_ascii_digit_regex(self):
        py = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertIn('r"[0-9]+"', py)
        self.assertNotIn(r"[+-]?\d+", py)
        form = (ROOT / "shell" / "Form1.cs").read_text(encoding="utf-8")
        self.assertIn('^[0-9]+$', form)
        for name in (
            "Start-HiveBrainComms.ps1",
            "Focus-Or-Start-Shell.ps1",
            "tools/Claim-BusJob.ps1",
            "tools/Approve-BusJob.ps1",
        ):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("^[0-9]+$", text, name)

    def test_build_html_506eebd_fixture_sha256(self):
        raw = _bytes_lf(BUILD_HTML_506EEBD)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), BUILD_HTML_506EEBD_SHA256)

    def test_build_html_is_19_approvals_plus_host_ws(self):
        raw = _bytes_lf(BUILD_HTML_13E904B)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), BUILD_HTML_13E904B_SHA256)
        current = (ROOT / "build.html").read_text(encoding="utf-8")
        self.assertIn("location.host", current)
        self.assertNotIn('id="approvals"', current)
        self.assertNotIn("loadApprovals", current)
        self.assertIn("currentToken", current)
        self.assertNotIn("ticket", current.lower())
        self.assertNotIn("ws://127.0.0.1:8107/term", current)

    def test_runtime_skip_is_narrow(self):
        text = (ROOT / "tests" / "runtime_skip.py").read_text(encoding="utf-8")
        for needle in (
            "access is denied",
            "permission denied",
            "winerror 5",
            "0x80070005",
        ):
            self.assertNotIn(needle, text.lower())
        self.assertIn("winerr == 1314", text)
        self.assertIn("def log_skip", text)
        helper = (ROOT / "tests" / "test_build_claim_1010.py").read_text(encoding="utf-8")
        self.assertNotIn("winerr in (5, 1314)", helper)

    def test_requirements_pins(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("aiohttp==3.14.5", text)
        self.assertIn("tzdata==2026.5", text)
        self.assertIn('pywinpty==2.0.15; sys_platform == "win32"', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
