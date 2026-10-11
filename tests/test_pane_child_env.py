"""Pane child env allowlist. Temporary mocks only. No secrets."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server
from tests.live_guard import install_live_state_guard

install_live_state_guard()

ALLOWED = frozenset(("GROK_HOME", "PATH") + server.WIN_CHILD_ENV)

PARENT = {
    "SYSTEMROOT": "X:\\Windows",
    "WINDIR": "X:\\Windows",
    "COMSPEC": "X:\\Windows\\System32\\cmd.exe",
    "TEMP": "X:\\Temp",
    "TMP": "X:\\Temp",
    "USERPROFILE": "X:\\profile",
    "LOCALAPPDATA": "X:\\local",
    "APPDATA": "X:\\roaming",
    "SECRET": "do-not-copy",
    "PATH": "X:\\evil",
    "USERNAME": "do-not-copy",
    "HBC_TOKEN": "do-not-copy",
}


class TestPaneChildEnv(unittest.TestCase):
    def test_posix_unchanged(self):
        home = Path("X") / "pane"
        with mock.patch.object(os, "name", "posix"), mock.patch.dict(os.environ, PARENT, clear=True):
            env = server.pane_child_env(home)
        self.assertEqual(env, {"GROK_HOME": str(home), "PATH": str(server.GROK.parent)})
        self.assertNotIn("SYSTEMROOT", env)
        self.assertNotIn("SECRET", env)

    def test_windows_allowlist_from_parent(self):
        home = Path("X") / "pane"
        with mock.patch.object(os, "name", "nt"), mock.patch.dict(os.environ, PARENT, clear=True):
            env = server.pane_child_env(home)
        self.assertEqual(env["GROK_HOME"], str(home))
        self.assertEqual(env["PATH"], str(server.GROK.parent))
        self.assertEqual(env["SYSTEMROOT"], "X:\\Windows")
        for key in server.WIN_CHILD_ENV:
            self.assertEqual(env.get(key), PARENT[key], key)
        self.assertTrue(set(env) <= ALLOWED)
        self.assertNotIn("SECRET", env)
        self.assertNotIn("USERNAME", env)
        self.assertNotIn("HBC_TOKEN", env)
        self.assertNotEqual(env["PATH"], PARENT["PATH"])

    def test_windows_skips_missing_allowlist_keys(self):
        home = Path("X") / "pane"
        partial = {"SYSTEMROOT": "X:\\Windows", "SECRET": "do-not-copy"}
        with mock.patch.object(os, "name", "nt"), mock.patch.dict(os.environ, partial, clear=True):
            env = server.pane_child_env(home)
        self.assertEqual(env["SYSTEMROOT"], "X:\\Windows")
        self.assertNotIn("WINDIR", env)
        self.assertNotIn("COMSPEC", env)
        self.assertNotIn("TEMP", env)
        self.assertNotIn("SECRET", env)
        self.assertTrue(set(env) <= ALLOWED)

    def test_run_pane_uses_allowlist_env(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            server.set_run_impl(None)
            signed = {
                "v": "2",
                "id": "env-1",
                "from": "Lead",
                "to": "Build",
                "lane": "lab",
                "priority": "normal",
                "time": "2026-10-10 12:00",
                "routed": "2026-10-10 12:01",
                "pane": "1",
                "job": "list lab pane files",
                "done-when": "the pane lists its files",
            }
            from hub import sigverify
            key = bytearray(os.urandom(32))
            sigverify.set_key_provider(lambda owner: bytearray(key) if owner == "Lead" else None)
            signed["sig"] = sigverify.mac_hex(signed, key)
            bus = Path(tmp.name) / "bus"
            bus.mkdir()
            (bus / "LEAD-TO-BUILD.md").write_text("", encoding="utf-8")
            prev_bus, prev_log, prev_seen, prev_grok = server.BUS, server.LOG, server.SEEN, server.GROK
            fake_grok = Path(tmp.name) / "grok.exe"
            fake_grok.write_bytes(b"")
            server.BUS = bus
            server.LOG = Path(tmp.name) / "approvals.jsonl"
            server.SEEN = Path(tmp.name) / ".seen-ids.txt"
            server.GROK = fake_grok
            try:
                real_env = server.pane_child_env

                def win_env(home):
                    with mock.patch.object(os, "name", "nt"):
                        return real_env(home)

                with mock.patch.object(server, "pane_child_env", side_effect=win_env), \
                        mock.patch.dict(os.environ, PARENT, clear=True), \
                        mock.patch("server.subprocess.run") as run:
                    run.return_value = mock.Mock(returncode=0, stdout="ok", stderr="")
                    code, _out, err = server.run_pane("1", signed["job"], signed, "Lead")
                self.assertEqual(code, 0)
                self.assertEqual(err, "")
                env = run.call_args.kwargs["env"]
                self.assertEqual(env["SYSTEMROOT"], "X:\\Windows")
                self.assertEqual(env["PATH"], str(fake_grok.parent))
                for key in server.WIN_CHILD_ENV:
                    self.assertEqual(env.get(key), PARENT[key], key)
                self.assertTrue(set(env) <= ALLOWED)
                self.assertNotIn("SECRET", env)
            finally:
                server.BUS = prev_bus
                server.LOG = prev_log
                server.SEEN = prev_seen
                server.GROK = prev_grok
                sigverify.clear_key_provider()
                sigverify._wipe(key)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
