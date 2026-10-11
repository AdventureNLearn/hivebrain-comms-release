"""Restrict .session-token and .sessions.json ACLs at hub start. Tmp ROOT only."""

import asyncio
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

import server
from tests.live_guard import install_live_state_guard, point_hub_at, restore_hub

install_live_state_guard()

LIVE_TOKEN = ROOT / ".session-token"
ACL_REMOVED = ("*S-1-5-11", "*S-1-5-32-545", "*S-1-1-0")


def _assert_icacls_lock(test, cmd, sid=None):
    test.assertEqual(cmd[0], "icacls")
    test.assertIn("/inheritance:r", cmd)
    inherit = cmd.index("/inheritance:r")
    for name in ACL_REMOVED:
        test.assertIn("/remove", cmd)
        test.assertIn(name, cmd)
        test.assertLess(inherit, cmd.index(name))
    test.assertNotIn("Authenticated Users", cmd)
    test.assertNotIn("Users", cmd)
    test.assertNotIn("Everyone", cmd)
    if sid:
        test.assertIn("*%s:(M)" % sid, cmd)
        test.assertIn("*S-1-5-18:(F)", cmd)


def _live_token_bytes():
    if not LIVE_TOKEN.is_file():
        return None
    return LIVE_TOKEN.read_bytes()


def _run_start_hub(home):
    prev_events = getattr(server, "EVENTS", None)
    try:
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
    finally:
        if prev_events is not None:
            server.EVENTS = prev_events


class TestSecretAcl(unittest.TestCase):
    def setUp(self):
        self.live_before = _live_token_bytes()
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.prev_hub = point_hub_at(self.home)
        self.prev_token = server.TOKEN
        server.TOKEN = ""

    def tearDown(self):
        server.TOKEN = self.prev_token
        restore_hub(self.prev_hub)
        self.tmp.cleanup()
        self.assertEqual(_live_token_bytes(), self.live_before)

    def test_01_noop_off_windows(self):
        if os.name == "nt" or server._secret_acl_windows():
            from tests.runtime_skip import log_skip
            log_skip(self, "test_01_noop_off_windows: secret ACL runs on Windows")
        token = self.home / ".session-token"
        sessions = self.home / ".sessions.json"
        token.write_text("abc", encoding="utf-8")
        sessions.write_text("{}", encoding="utf-8")
        with patch.object(server.subprocess, "run") as run:
            server.restrict_secret_state_acl()
            run.assert_not_called()
        self.assertEqual(token.read_text(encoding="utf-8"), "abc")
        self.assertEqual(sessions.read_text(encoding="utf-8"), "{}")

    def test_02_windows_icacls_sid_and_system(self):
        sid = "S-1-5-21-1-2-3-1001"
        (self.home / ".session-token").write_text("tok", encoding="utf-8")
        (self.home / ".sessions.json").write_text("{}", encoding="utf-8")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(server, "_secret_acl_windows", return_value=True), patch.object(
            server, "_current_user_sid", return_value=sid
        ), patch.dict(os.environ, {"USERNAME": "labuser"}), patch.object(
            server.subprocess, "run", fake_run
        ):
            server.restrict_secret_state_acl()
        names = [Path(cmd[1]).name for cmd in calls]
        self.assertEqual(names, [".session-token", ".sessions.json"])
        for cmd in calls:
            joined = " ".join(cmd)
            _assert_icacls_lock(self, cmd, sid)
            self.assertNotIn("*%s:(F)" % sid, cmd)
            self.assertNotIn("labuser:(R,W)", cmd)
            self.assertNotIn("SYSTEM:(F)", cmd)
            self.assertNotIn(str(ROOT / ".session-token"), joined)

        src = Path(server.__file__).read_text(encoding="utf-8")
        acl_fn = src.split("def _restrict_one_secret_acl", 1)[1].split("\ndef ", 1)[0]
        self.assertIn('"/remove"', acl_fn)
        self.assertIn('"*S-1-5-11"', acl_fn)
        self.assertIn('"*S-1-5-32-545"', acl_fn)
        self.assertIn('"*S-1-1-0"', acl_fn)
        self.assertNotIn('"Authenticated Users"', acl_fn)
        self.assertNotIn('"Users"', acl_fn)
        self.assertNotIn('"Everyone"', acl_fn)
        self.assertLess(acl_fn.find("/inheritance:r"), acl_fn.find("/remove"))
        self.assertIn("_current_user_sid", acl_fn)
        self.assertIn("ConvertSidToStringSidW", src)
        self.assertIn("GetTokenInformation", src)
        self.assertIn('SYSTEM_SID = "S-1-5-18"', src)
        self.assertIn('"*%s:(M)" % sid', src)
        self.assertIn('"*%s:(F)" % SYSTEM_SID', src)
        self.assertNotIn('"*%s:(F)" % sid', src)
        self.assertNotIn("USERNAME", acl_fn)
        self.assertNotIn("labuser:(R,W)", acl_fn)
        self.assertNotIn("SYSTEM:(F)", acl_fn)

    def test_03_missing_sessions_skipped(self):
        (self.home / ".session-token").write_text("tok", encoding="utf-8")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(server, "_secret_acl_windows", return_value=True), patch.object(
            server, "_current_user_sid", return_value="S-1-5-21-1-2-3-1001"
        ), patch.object(server.subprocess, "run", fake_run):
            server.restrict_secret_state_acl()
        self.assertEqual([Path(cmd[1]).name for cmd in calls], [".session-token"])

    def test_04_icacls_failure_does_not_stop_hub(self):
        def boom(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, "", "access denied")

        logs = io.StringIO()
        with patch.object(server, "_secret_acl_windows", return_value=True), patch.object(
            server, "_current_user_sid", return_value="S-1-5-21-1-2-3-1001"
        ), patch.object(server.subprocess, "run", boom), patch("sys.stdout", logs):
            _run_start_hub(self.home)
        self.assertTrue(server.TOKEN)
        self.assertTrue((self.home / ".session-token").is_file())
        self.assertIn("secret-acl warning", logs.getvalue())

    def test_05_start_hub_restricts_after_mint(self):
        order = []

        def mint():
            order.append("mint")
            server.TOKEN = "a" * 32
            (self.home / ".session-token").write_text(server.TOKEN, encoding="utf-8")

        def restrict():
            order.append("acl")

        with patch.object(server, "ensure_session_token", mint), patch.object(
            server, "restrict_secret_state_acl", restrict
        ):
            _run_start_hub(self.home)
        self.assertEqual(order[:2], ["mint", "acl"])

    def test_05b_existing_sessions_reacl_on_first_start(self):
        sid = "S-1-5-21-1-2-3-1001"
        sessions = self.home / ".sessions.json"
        sessions.write_text('{"sessions":[],"open_build":[]}', encoding="utf-8")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(server, "_secret_acl_windows", return_value=True), patch.object(
            server, "_current_user_sid", return_value=sid
        ), patch.object(server.subprocess, "run", fake_run):
            _run_start_hub(self.home)
        names = [Path(cmd[1]).name for cmd in calls]
        self.assertIn(".sessions.json", names)
        session_cmds = [cmd for cmd in calls if Path(cmd[1]).name == ".sessions.json"]
        self.assertTrue(session_cmds)
        for cmd in session_cmds:
            _assert_icacls_lock(self, cmd, sid)
        src = Path(server.__file__).read_text(encoding="utf-8")
        start = src.split("async def start_hub", 1)[1].split("\ndef ", 1)[0]
        restrict = src.split("def restrict_secret_state_acl", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("restrict_secret_state_acl()", start)
        self.assertIn("secret_state_paths()", restrict)
        self.assertIn("_restrict_one_secret_acl(path)", restrict)

    def test_06_sessions_atomic_write_reapplies_acl(self):
        sid = "S-1-5-21-1-2-3-1001"
        sessions = self.home / ".sessions.json"
        other = self.home / "other.json"
        calls = []
        sizes = []

        def fake_run(cmd, **kwargs):
            sizes.append(Path(cmd[1]).stat().st_size)
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(server, "_secret_acl_windows", return_value=True), patch.object(
            server, "_current_user_sid", return_value=sid
        ), patch.object(server.subprocess, "run", fake_run):
            server._atomic_write_json(sessions, {"sessions": [], "open_build": []})
            before_other = len(calls)
            server._atomic_write_json(other, {"x": 1})
        self.assertEqual(len(calls), before_other)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sizes, [0])
        finals = [cmd for cmd in calls if Path(cmd[1]).name == ".sessions.json"]
        temps = [
            cmd
            for cmd in calls
            if Path(cmd[1]).name.startswith(".sessions.json.")
            and Path(cmd[1]).name.endswith(".tmp")
        ]
        self.assertEqual(len(finals), 0)
        self.assertEqual(len(temps), 1)
        for cmd in calls:
            _assert_icacls_lock(self, cmd, sid)
            self.assertNotIn("*%s:(F)" % sid, cmd)
            self.assertNotIn("labuser:(R,W)", cmd)
        self.assertTrue(sessions.is_file())
        self.assertGreater(sessions.stat().st_size, 0)

        src = Path(server.__file__).read_text(encoding="utf-8")
        write_fn = src.split("def _atomic_write_json", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("_maybe_restrict_secret_acl(tmp)", write_fn)
        self.assertNotIn("_maybe_restrict_secret_acl(path)", write_fn)
        self.assertLess(
            write_fn.find("_maybe_restrict_secret_acl(tmp)"),
            write_fn.find("json.dump"),
        )

    def test_07_icacls_stderr_paths_scrubbed_from_events(self):
        token = self.home / ".session-token"
        token.write_text("tok", encoding="utf-8")
        leaked = str(token)
        events = self.home / ".hub-events.jsonl"
        prev = server.EVENTS

        def boom(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, 1, "", "Access denied: %s" % leaked
            )

        try:
            server.EVENTS = events
            with patch.object(server, "_secret_acl_windows", return_value=True), patch.object(
                server, "_current_user_sid", return_value="S-1-5-21-1-2-3-1001"
            ), patch.object(server.subprocess, "run", boom):
                server.restrict_secret_state_acl()
            text = events.read_text(encoding="utf-8")
        finally:
            server.EVENTS = prev
        self.assertIn("secret-acl-warn", text)
        self.assertNotIn(leaked, text)
        self.assertNotIn(str(self.home), text)
        self.assertNotIn(str(ROOT), text)

        win = r"C:\Users\lab\AppData\Hive\.sessions.json: Access is denied."
        posix = "/tmp/abc/.sessions.json: Access is denied."
        scrubbed_win = server._scrub_acl_text(win, name=".sessions.json")
        scrubbed_posix = server._scrub_acl_text(
            posix, name=".sessions.json", path="/tmp/abc/.sessions.json"
        )
        self.assertNotIn(r"C:\Users", scrubbed_win)
        self.assertNotIn("AppData", scrubbed_win)
        self.assertNotIn("/tmp/", scrubbed_posix)
        self.assertIn(".sessions.json", scrubbed_win)
        self.assertIn(".sessions.json", scrubbed_posix)

    def test_09_scrub_paths_with_spaces(self):
        win = r"C:\Users\Smith Jones\AppData\Hive\.sessions.json: Access is denied."
        win_dots = r"C:\Users\Smith ...\AppData\Hive\.sessions.json: Access is denied."
        posix = "/home/Smith Jones/.sessions.json: Access is denied."
        quoted = r'"C:\Users\Smith Jones\Hive\.sessions.json": Access is denied.'
        for raw in (win, win_dots, posix, quoted):
            scrubbed = server._scrub_acl_text(raw, name=".sessions.json")
            self.assertNotIn("Smith", scrubbed, raw)
            self.assertNotIn("Jones", scrubbed, raw)
            self.assertNotIn("AppData", scrubbed, raw)
            self.assertNotIn("/home/", scrubbed, raw)
            self.assertNotIn(r"C:\Users", scrubbed, raw)
            self.assertIn(".sessions.json", scrubbed, raw)

    def test_08_missing_sid_does_not_grant_username(self):
        (self.home / ".session-token").write_text("tok", encoding="utf-8")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(server, "_secret_acl_windows", return_value=True), patch.object(
            server, "_current_user_sid", return_value=""
        ), patch.dict(os.environ, {"USERNAME": "labuser", "USER": "labuser"}), patch.object(
            server.subprocess, "run", fake_run
        ):
            server.restrict_secret_state_acl()
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
