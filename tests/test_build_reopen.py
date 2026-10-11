"""Build watcher reopen after a hub restart. Linux fake-spawner tests."""

import asyncio
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server
from tests.live_guard import install_live_state_guard, point_hub_at, restore_hub
from hub.win_child_env import WIN_CHILD_ENV as SHARED_WIN_CHILD_ENV

install_live_state_guard()

ALLOWED = frozenset(("GROK_HOME", "PATH", "PROMPT") + server.WIN_CHILD_ENV + server.TERM_EXTRA_ENV)
AUTH_KEYS = frozenset(server.GROK_AUTH_KEYS)
ON = {"HBC_BUILD_AUTOREOPEN": "1"}


class FakePty:
    def __init__(self):
        self.writes = []
        self.alive = True
        self.pid = 4242

    def write(self, data):
        self.writes.append(data)

    def isalive(self):
        return self.alive

    def close(self):
        self.alive = False

    @property
    def returncode(self):
        return 0 if not self.alive else None


class FakeSpawner:
    def __init__(self):
        self.calls = []

    def __call__(self, pane, env, cwd):
        pty = FakePty()
        self.calls.append({"pane": pane, "env": env, "cwd": cwd, "pty": pty})
        return pty


class TestBuildReopen(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._sessions = server.SESSIONS
        self._events = server.EVENTS
        self._panes = dict(server.PANES)
        self._broken = server._sessions_broken
        self._exit_logged = dict(server._exit_logged)
        server.SESSIONS = Path(self.tmp.name) / ".sessions.json"
        server.EVENTS = Path(self.tmp.name) / ".hub-events.jsonl"
        server.PANES.clear()
        server._exit_logged.clear()
        server._sessions_broken = False
        self.spawner = FakeSpawner()
        server.set_spawn_impl(self.spawner)

    def tearDown(self):
        server.set_spawn_impl(None)
        server.PANES.clear()
        server.PANES.update(self._panes)
        server._exit_logged.clear()
        server._exit_logged.update(self._exit_logged)
        server.SESSIONS = self._sessions
        server.EVENTS = self._events
        server._sessions_broken = self._broken
        self.tmp.cleanup()

    def _written(self, index):
        return "".join(self.spawner.calls[index]["pty"].writes)

    def _record_build(self):
        server.ensure_pane("build")
        server.PANES.clear()

    def test_restart_reopens_build_once_with_resume_prompt(self):
        opened = server.ensure_pane("build")
        self.assertTrue(opened)
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertEqual(self.spawner.calls[0]["pane"], "build")
        first = self._written(0)
        self.assertEqual(first, server.pane_boot_command("build"))
        self.assertIn(server.BUS_WATCH_PROMPT, first)

        server.PANES.clear()
        logs = []
        with patch("builtins.print") as printer:
            printer.side_effect = lambda *args, **kwargs: logs.append((args, kwargs))
            server.reopen_build_sessions(environ=ON)
            server.reopen_build_sessions(environ=ON)

        self.assertEqual(len(self.spawner.calls), 2)
        self.assertEqual(self.spawner.calls[1]["pane"], "build")
        self.assertEqual(self._written(1), server.pane_boot_command("build"))
        reopen_lines = [args[0] for args, _kwargs in logs if args and args[0] == "pane reopen build"]
        self.assertEqual(reopen_lines, ["pane reopen build"])

    def test_no_reopen_when_no_session_recorded(self):
        logs = []
        with patch("builtins.print") as printer:
            printer.side_effect = lambda *args, **kwargs: logs.append(args[0] if args else "")
            server.reopen_build_sessions(environ=ON)
        self.assertEqual(self.spawner.calls, [])
        self.assertEqual(server.PANES, {})
        self.assertFalse(any(str(line).startswith("pane reopen") for line in logs))

    def test_no_reopen_when_autoreopen_off(self):
        self._record_build()
        self.assertEqual(len(self.spawner.calls), 1)
        server.reopen_build_sessions()
        server.reopen_build_sessions(environ={"HBC_BUILD_AUTOREOPEN": "0"})
        server.reopen_build_sessions(environ={"HBC_BUILD_AUTOREOPEN": "false"})
        self.assertEqual(len(self.spawner.calls), 1)
        self.assertFalse(server.build_autoreopen_enabled({}))

    def test_closed_pane_is_removed_from_open_build(self):
        server.ensure_pane("build")
        self.assertEqual(server.open_build_names(), ["build"])
        server.close_pane("build")
        self.assertEqual(server.open_build_names(), [])
        server.reopen_build_sessions(environ=ON)
        self.assertEqual(len(self.spawner.calls), 1)

    def test_reopen_env_matches_term_child_allowlist(self):
        src = Path(server.__file__).read_text(encoding="utf-8")
        self.assertNotIn("os.environ.copy()", src)
        # PR #2 keeps pane_child_env(home) for run_pane. Reopen must use term_child_env.
        self.assertIsNotNone(re.search(r"^def pane_child_env\(home\)", src, re.M))
        self.assertIn("child_env = term_child_env(home, pane)", src)
        self.assertIn("from hub.win_child_env import WIN_CHILD_ENV", src)
        self.assertIs(server.WIN_CHILD_ENV, SHARED_WIN_CHILD_ENV)

        self._record_build()
        server.reopen_build_sessions(environ=ON)
        self.assertEqual(len(self.spawner.calls), 2)
        call = self.spawner.calls[1]
        home = server.ROOT / "panes" / "pane-build"
        expected = server.term_child_env(home, "build")
        self.assertEqual(call["env"], expected)
        self.assertTrue(set(call["env"]) <= ALLOWED | AUTH_KEYS)
        self.assertNotIn("SECRET", call["env"])

    def test_valid_build_pane_is_exact_set(self):
        self.assertTrue(server.valid_build_pane("build"))
        self.assertTrue(server.valid_build_pane("build2"))
        self.assertTrue(server.valid_build_pane("build8"))
        self.assertTrue(server.valid_term_pane("1"))
        self.assertTrue(server.valid_term_pane("build"))
        self.assertFalse(server.valid_build_pane("build02"))
        self.assertFalse(server.valid_term_pane("build02"))
        self.assertFalse(server.valid_build_pane("build٢"))
        self.assertFalse(server.valid_term_pane("build٢"))
        self.assertFalse(server.valid_build_pane("build²"))
        self.assertFalse(server.valid_term_pane("build²"))
        self.assertTrue("²".isdigit())
        self.assertTrue("٢".isdigit())

    def test_corrupt_sessions_file_is_left_alone(self):
        cases = {
            "corrupt": b"{not-json",
            "undecodable": b"\xff\xfe\x00not-utf8",
            "oversized": b"{" + (b"x" * (server.SESSIONS_MAX_BYTES + 8)),
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                server.PANES.clear()
                server._sessions_broken = False
                server.SESSIONS.write_bytes(payload)
                before = server.SESSIONS.read_bytes()
                server.reopen_build_sessions(environ=ON)
                server.remember("1", "job-x", "ok")
                server.remember_open_build("build")
                self.assertEqual(server.SESSIONS.read_bytes(), before)
                self.assertEqual(self.spawner.calls, [])
                self.assertTrue(server._sessions_broken)

    def test_sessions_writes_are_atomic_under_lock(self):
        replaces = []
        real_replace = os.replace

        def counted_replace(src, dst):
            replaces.append((src, dst))
            return real_replace(src, dst)

        errors = []

        def writer(index):
            try:
                server.remember("1", "job-%s" % index, "ok")
                server.remember_open_build("build")
                server.forget_open_build("build")
                server.remember_open_build("build2")
            except Exception as exc:
                errors.append(exc)

        with patch("os.replace", side_effect=counted_replace):
            threads = [threading.Thread(target=writer, args=(i,)) for i in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(errors, [])
        self.assertGreater(len(replaces), 0)
        for _src, dst in replaces:
            self.assertEqual(Path(dst), server.SESSIONS)
        data = json.loads(server.SESSIONS.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        self.assertIsInstance(data.get("sessions"), list)
        self.assertIsInstance(data.get("open_build"), list)
        self.assertIn("build2", data["open_build"])

    def test_reopen_follows_successful_bind(self):
        self._record_build()
        order = []

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

        def reopen(environ=None):
            order.append("reopen")

        prev_hub = point_hub_at(self.tmp.name)
        try:
            with patch.object(web, "AppRunner", Runner), patch.object(web, "TCPSite", Site), patch.object(
                server, "reopen_build_sessions", reopen
            ), patch.object(server, "ensure_session_token"):
                asyncio.run(server.start_hub())
        finally:
            restore_hub(prev_hub)
        self.assertEqual(order, ["setup", "site", "bind", "reopen"])
        src = Path(server.__file__).read_text(encoding="utf-8")
        self.assertNotIn("Running on", src)
        self.assertNotIn("on_hub_print", src)

    def test_after_port_bound_reopens_once(self):
        self._record_build()
        previous = os.environ.get("HBC_BUILD_AUTOREOPEN")
        os.environ["HBC_BUILD_AUTOREOPEN"] = "1"
        try:
            server.after_port_bound()
            server.after_port_bound()
        finally:
            if previous is None:
                os.environ.pop("HBC_BUILD_AUTOREOPEN", None)
            else:
                os.environ["HBC_BUILD_AUTOREOPEN"] = previous
        self.assertEqual(len(self.spawner.calls), 2)
        self.assertEqual(self._written(1), server.pane_boot_command("build"))

    def test_prompt_is_shared_start_all_constant(self):
        self.assertIn("rules 14-18", server.BUS_WATCH_PROMPT)
        self.assertIn("rule 3 and rule 11 amended", server.BUS_WATCH_PROMPT)
        self.assertIn("Do not re-run a finished job.", server.BUS_WATCH_PROMPT)
        command = server.pane_boot_command("build")
        self.assertEqual(command, "grok --continue \"" + server.BUS_WATCH_PROMPT + "\"\r\n")
        server.ensure_pane("build")
        self.assertEqual(self._written(0), command)
        src = Path(server.__file__).read_text(encoding="utf-8")
        lanes_src = (Path(server.__file__).resolve().parent / "hub" / "lanes.py").read_text(encoding="utf-8")
        self.assertIn("lanes.watcher_prompt", src)
        self.assertEqual(src.count("Resume the handoff-bus watch."), 0)
        self.assertEqual(lanes_src.count("Resume the handoff-bus watch."), 1)

    def test_event_log_hub_restart_and_real_exit_code(self):
        server.log_hub_event("hub restart")
        server.ensure_pane("build")
        server.close_pane("build")
        rows = [json.loads(line) for line in server.EVENTS.read_text(encoding="utf-8").splitlines() if line.strip()]
        kinds = [row["event"] for row in rows]
        self.assertEqual(kinds.count("hub restart"), 1)
        self.assertEqual(kinds.count("build exit"), 1)
        ended = next(row for row in rows if row["event"] == "build exit")
        self.assertEqual(ended.get("pane"), "build")
        self.assertIsInstance(ended.get("exit"), int)
        self.assertEqual(ended.get("exit"), 0)
        self.assertIsNotNone(ended.get("exit"))
        text = server.EVENTS.read_text(encoding="utf-8")
        self.assertNotIn("SECRET", text)
        self.assertNotIn(str(Path.home()), text)
        self.assertNotIn("null", text)

    def test_note_pane_exit_skips_when_pane_is_gone(self):
        server.ensure_pane("build")
        server.note_pane_exit("build")
        server.note_pane_exit("build")
        server.close_pane("build")
        rows = [json.loads(line) for line in server.EVENTS.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual([row["event"] for row in rows].count("build exit"), 1)

    def test_note_pane_exit_uses_hub_flag_not_pty_attr(self):
        class SlottedPty:
            __slots__ = ("pid",)

            def __init__(self):
                self.pid = os.getpid()

            def isalive(self):
                return False

        pty = SlottedPty()
        self.assertFalse(hasattr(pty, "_exit_logged"))
        server.PANES["build"] = pty
        server.remember_open_build("build")
        server.note_pane_exit("build")
        self.assertFalse(hasattr(pty, "_exit_logged"))
        self.assertTrue(server._exit_logged.get("build"))
        rows = [json.loads(line) for line in server.EVENTS.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual([row["event"] for row in rows].count("build exit"), 1)
        server.PANES["build"] = SlottedPty()
        server.note_pane_exit("build")
        rows = [json.loads(line) for line in server.EVENTS.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual([row["event"] for row in rows].count("build exit"), 1)
        server.ensure_pane("build")
        self.assertNotIn("build", server._exit_logged)
        server.note_pane_exit("build")
        rows = [json.loads(line) for line in server.EVENTS.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual([row["event"] for row in rows].count("build exit"), 2)
        src = Path(server.__file__).read_text(encoding="utf-8")
        self.assertNotIn("pty._exit_logged", src)
        self.assertIn("_exit_logged[pane] = True", src)

    def test_unknown_exit_is_null_not_minus_one(self):
        class BarePty:
            pid = os.getpid()

            def isalive(self):
                return False

        server.PANES["build"] = BarePty()
        server.remember_open_build("build")
        server.note_pane_exit("build")
        rows = [json.loads(line) for line in server.EVENTS.read_text(encoding="utf-8").splitlines() if line.strip()]
        ended = next(row for row in rows if row["event"] == "build exit")
        self.assertIsNone(ended.get("exit"))
        self.assertNotEqual(ended.get("exit"), -1)
        self.assertIsNone(server.pane_exit_code(BarePty()))
        self.assertNotIn("process_exit_code", Path(server.__file__).read_text(encoding="utf-8"))

    def test_event_log_rotates_and_keeps_old_files(self):
        server.EVENTS_MAX_BYTES = 80
        try:
            server.log_hub_event("hub restart")
            for index in range(8):
                server.log_hub_event("build exit", pane="build", exit=index)
            self.assertTrue(server.EVENTS.is_file())
            first = server.EVENTS.with_name(server.EVENTS.name + ".1")
            second = server.EVENTS.with_name(server.EVENTS.name + ".2")
            self.assertTrue(first.is_file())
            kept = [server.EVENTS.name]
            if first.is_file():
                kept.append(first.name)
            if second.is_file():
                kept.append(second.name)
            self.assertLessEqual(len(kept), 3)
            third = server.EVENTS.with_name(server.EVENTS.name + ".3")
            self.assertFalse(third.exists())
        finally:
            server.EVENTS_MAX_BYTES = 64 * 1024


if __name__ == "__main__":
    unittest.main(verbosity=2)
