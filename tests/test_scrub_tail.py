"""Error-tail scrubbing. Fake paths and names only."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hub import protocol, sigverify
import server
from tests.live_guard import install_live_state_guard

install_live_state_guard()

WIN_PATH = "C:\\Users\\agentid\\AppData\\Local\\Temp\\fail.txt"
POSIX_PATH = "/home/agentid/.cache/fail.txt"
USER = "agentid"
ENV_KEY = "HBCFAKE"
ENV_VAL = "abcdef"
PROFILE = "X:\\Profiles\\agentid"


def _sign(fields, key):
    mac = sigverify.mac_hex(fields, key)
    out = dict(fields)
    out["sig"] = mac
    return out


def _iso():
    return datetime.now(ZoneInfo("America/New_York")).replace(microsecond=0).isoformat()


def _job(**overrides):
    iso = _iso()
    fields = {
        "v": "2",
        "id": "scrub-1",
        "from": "Lead",
        "to": "Build",
        "lane": "lab",
        "priority": "normal",
        "time": iso,
        "routed": "Lead " + iso,
        "pane": "1",
        "job": "list lab pane files",
        "done-when": "the pane lists its files",
    }
    fields.update(overrides)
    return fields


def _write_fake_grok(directory, source):
    """Grok stand-in that runs on Windows and Linux.

    Windows CreateProcess rejects a shebang file (WinError 193). Write a
    .cmd that calls sys.executable, or a shebang script on POSIX.
    """
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


def _write_signed(bus, fields, key):
    signed = _sign(fields, key)
    (bus / "LEAD-TO-BUILD.md").write_text(
        "\n".join(f"{name}: {signed[name]}" for name in (
            "id", "v", "from", "to", "lane", "priority", "time", "routed",
            "pane", "job", "done-when", "sig",
        )) + "\n",
        encoding="utf-8",
    )
    return signed


class TestScrubTail(unittest.TestCase):
    def test_windows_users_path(self):
        out = server.scrub_tail("boom at " + WIN_PATH)
        self.assertNotIn("Users\\agentid", out)
        self.assertNotIn("Users/agentid", out)
        self.assertIn("<home>", out)

    def test_posix_home_path(self):
        out = server.scrub_tail("boom at " + POSIX_PATH)
        self.assertNotIn("/home/agentid", out)
        self.assertIn("<home>", out)

    def test_actual_home_dir(self):
        home = str(Path.home())
        out = server.scrub_tail("boom at " + home + "/cache")
        self.assertNotIn(home, out)
        self.assertIn("<home>", out)

    def test_username(self):
        with mock.patch.dict(os.environ, {"USERNAME": USER, "USER": USER}, clear=False):
            out = server.scrub_tail("user=" + USER + " failed")
        self.assertNotIn(USER, out)
        self.assertIn("<user>", out)

    def test_env_value_uses_generic_placeholder(self):
        with mock.patch.dict(os.environ, {ENV_KEY: ENV_VAL}, clear=False):
            out = server.scrub_tail("token " + ENV_VAL + " leaked")
        self.assertNotIn(ENV_VAL, out)
        self.assertIn("<env>", out)
        self.assertNotIn(ENV_KEY, out)
        self.assertNotIn("<" + ENV_KEY + ">", out)

    def test_keeps_tail_not_head_lines(self):
        raw = "HEAD-UNIQUE\n" + "\n".join("mid-%s" % index for index in range(80)) + "\nTAIL-UNIQUE"
        out = server.scrub_tail(raw)
        self.assertNotIn("HEAD-UNIQUE", out)
        self.assertNotIn("mid-0", out)
        self.assertIn("TAIL-UNIQUE", out)
        self.assertIn("mid-79", out)
        self.assertLessEqual(len(out.splitlines()), server.TAIL_MAX_LINES + 1)
        self.assertIn("<truncated>", out)

    def test_keeps_tail_not_head_chars(self):
        raw = "HEAD-UNIQUE" + ("x" * 5000) + "TAIL-UNIQUE"
        out = server.scrub_tail(raw)
        self.assertNotIn("HEAD-UNIQUE", out)
        self.assertIn("TAIL-UNIQUE", out)
        self.assertLessEqual(len(out), server.TAIL_MAX_CHARS + len("\n<truncated>"))
        self.assertIn("<truncated>", out)

    def test_literal_home_and_user_first_ci_slash_unc_json(self):
        env = {"USERPROFILE": PROFILE, "USERNAME": "Agentid", "USER": "Agentid"}
        samples = [
            r"see x:/profiles/agentid/file",
            r"see X:\\Profiles\\Agentid\\file",
            r"see \\nas\Profiles\Agentid\file",
            "see x:%5Cprofiles%5Cagentid%5Cfile",
            "see x:%2fprofiles%2fagentid%2fsecret",
            r"see //nas/Users/Agentid/docs",
            r"see %5C%5Cnas%5CUsers%5Cagentid%5Cdocs",
        ]
        with mock.patch.dict(os.environ, env, clear=False):
            for raw in samples:
                out = server.scrub_tail(raw)
                self.assertNotIn("agentid", out.lower(), raw)
                self.assertNotIn("Agentid", out, raw)

    def test_userprofile_parent_is_scrubbed(self):
        with mock.patch.dict(os.environ, {"USERPROFILE": PROFILE}, clear=False):
            out = server.scrub_tail(r"see X:\Profiles\other\leak")
        self.assertNotIn(r"X:\Profiles", out)
        self.assertNotIn("Profiles\\other", out)
        self.assertIn("<home>", out)

    def test_slash_run_does_not_redos(self):
        raw = "/" * 100000
        start = time.perf_counter()
        server.scrub_tail(raw)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_return_file_sessions_and_api_use_scrubbed_stdout(self):
        tmp = tempfile.TemporaryDirectory()
        key = bytearray(os.urandom(32))
        try:
            home = Path(tmp.name)
            bus = home / "bus"
            bus.mkdir()
            for name in ("LEAD-TO-BUILD.md", "BUILD-TO-LEAD.md"):
                (bus / name).write_text("", encoding="utf-8")
            prev_bus, prev_log, prev_seen, prev_root = server.BUS, server.LOG, server.SEEN, server.ROOT
            prev_sessions = server.SESSIONS
            prev_events = getattr(server, "EVENTS", None)
            prev_broken = getattr(server, "_sessions_broken", False)
            server.BUS = bus
            server.LOG = home / "approvals.jsonl"
            server.SEEN = home / ".seen-ids.txt"
            server.ROOT = home
            server.SESSIONS = home / ".sessions.json"
            if prev_events is not None:
                server.EVENTS = home / ".hub-events.jsonl"
            server._sessions_broken = False
            protocol.set_approvals_log(server.LOG)
            protocol.read_seen(server.SEEN)
            raw = "boom at " + WIN_PATH + " user=" + USER + " token=" + ENV_VAL
            server.set_run_impl(lambda pane, command: (1, raw, ""))
            sigverify.set_key_provider(lambda owner: bytearray(key) if owner == "Lead" else None)
            signed = _sign(_job(id="scrub-out-1"), key)
            (bus / "LEAD-TO-BUILD.md").write_text(
                "\n".join(f"{name}: {signed[name]}" for name in (
                    "id", "v", "from", "to", "lane", "priority", "time", "routed",
                    "pane", "job", "done-when", "sig",
                )) + "\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"USERNAME": USER, "USER": USER, ENV_KEY: ENV_VAL}, clear=False):
                status, payload = server.apply_decision("scrub-out-1", "approve")
                state = server.state_payload()
            self.assertEqual(status, 200)
            returned = (bus / "BUILD-TO-LEAD.md").read_text(encoding="utf-8")
            log_text = server.LOG.read_text(encoding="utf-8") if server.LOG.is_file() else ""
            sessions = server.SESSIONS.read_text(encoding="utf-8") if server.SESSIONS.is_file() else ""
            for blob in (returned, log_text, sessions, json.dumps(payload), json.dumps(state)):
                self.assertNotIn(WIN_PATH, blob)
                self.assertNotIn(USER, blob)
                self.assertNotIn(ENV_VAL, blob)
            self.assertIn("<home>", returned)
            self.assertIn("<env>", returned)
            self.assertNotIn("<" + ENV_KEY + ">", returned)
            for row in (log_text.splitlines() if log_text else []):
                if row.strip():
                    data = json.loads(row)
                    self.assertNotIn("err", data)
                    self.assertNotIn("stderr", data)
                    self.assertNotIn("tail", data)
        finally:
            server.set_run_impl(None)
            sigverify.clear_key_provider()
            sigverify._wipe(key)
            server.BUS, server.LOG, server.SEEN, server.ROOT = prev_bus, prev_log, prev_seen, prev_root
            server.SESSIONS = prev_sessions
            if prev_events is not None:
                server.EVENTS = prev_events
            server._sessions_broken = prev_broken
            tmp.cleanup()

    def test_subprocess_stdout_is_scrubbed(self):
        tmp = tempfile.TemporaryDirectory()
        key = bytearray(os.urandom(32))
        prev_grok = server.GROK
        prev_bus, prev_log, prev_seen, prev_root = server.BUS, server.LOG, server.SEEN, server.ROOT
        try:
            home = Path(tmp.name)
            bus = home / "bus"
            bus.mkdir()
            for name in ("LEAD-TO-BUILD.md", "BUILD-TO-LEAD.md"):
                (bus / name).write_text("", encoding="utf-8")
            grok = _write_fake_grok(
                home,
                "import sys\n"
                "sys.stdout.write(r'home=X:\\Profiles\\agentid\\ok' + '\\n')\n",
            )
            server.GROK = grok
            server.BUS = bus
            server.LOG = home / "approvals.jsonl"
            server.SEEN = home / ".seen-ids.txt"
            server.ROOT = home
            server.set_run_impl(None)
            server.set_test_grok_argv(True)
            signed = _sign(_job(id="scrub-sub-1"), key)
            sigverify.set_key_provider(lambda owner: bytearray(key) if owner == "Lead" else None)
            with mock.patch.dict(
                os.environ,
                {"USERPROFILE": PROFILE, "USERNAME": USER, "USER": USER},
                clear=False,
            ):
                code, out, err = server.run_pane("1", signed["job"], signed, "Lead")
            self.assertEqual(code, 0)
            self.assertNotIn("agentid", out)
            self.assertNotIn("agentid", err)
            self.assertNotIn(r"X:\Profiles", out)
            self.assertIn("<home>", out)
        finally:
            server.set_run_impl(None)
            server.set_test_grok_argv(False)
            server.GROK = prev_grok
            sigverify.clear_key_provider()
            sigverify._wipe(key)
            server.BUS, server.LOG, server.SEEN, server.ROOT = prev_bus, prev_log, prev_seen, prev_root
            tmp.cleanup()

    def test_short_name_a_only_as_path_segment(self):
        with mock.patch.dict(os.environ, {"USERNAME": "a", "USER": "a"}, clear=False):
            with mock.patch("getpass.getuser", return_value="a"):
                out = server.scrub_tail(r"user=a path=C:\Users\a\x cat")
        self.assertIn("user=a", out)
        self.assertNotIn(r"\a\x", out)
        self.assertIn("cat", out)

    def test_short_name_e_skips_words_and_placeholders(self):
        with mock.patch.dict(os.environ, {"USERNAME": "e", "USER": "e"}, clear=False):
            with mock.patch("getpass.getuser", return_value="e"):
                out = server.scrub_tail(r"user=e the <home> scene path=\e\x")
        self.assertIn("user=e", out)
        self.assertIn("the", out)
        self.assertIn("<home>", out)
        self.assertNotIn("<<user>>", out)
        self.assertNotIn("<hom<user>>", out)
        self.assertNotIn(r"\e\x", out)

    def test_short_name_home_does_not_rewrite_placeholder(self):
        with mock.patch.dict(os.environ, {"USERNAME": "home", "USER": "home"}, clear=False):
            with mock.patch("getpass.getuser", return_value="home"):
                out = server.scrub_tail(r"user=home dir=<home> path=\home\x")
        self.assertIn("user=<user>", out)
        self.assertIn("<home>", out)
        self.assertNotIn("<<user>>", out)
        self.assertNotIn("<hom<user>>", out)
        self.assertNotIn(r"\home\x", out)

    def test_short_name_admin_path_segment_only(self):
        with mock.patch.dict(os.environ, {"USERNAME": "admin", "USER": "admin"}, clear=False):
            with mock.patch("getpass.getuser", return_value="admin"):
                out = server.scrub_tail(r"user=admin path=C:\Users\admin\x")
        self.assertIn("user=admin", out)
        self.assertNotIn(r"\admin\x", out)
        self.assertEqual(server.NAME_WORD_MIN, 3)
        self.assertIn("admin", server.GENERIC_USERNAMES)

    def test_test_username_keeps_testing_and_test_x(self):
        with mock.patch.dict(os.environ, {"USERNAME": "Test", "USER": "Test"}, clear=False):
            with mock.patch("getpass.getuser", return_value="Test"):
                out = server.scrub_tail(r"Testing test_x user=Test path=C:\Users\Test\x")
        self.assertIn("Testing", out)
        self.assertIn("test_x", out)
        self.assertNotIn(r"\Test\x", out)

    def test_three_char_username_is_word(self):
        name = "xyz"
        self.assertEqual(len(name), 3)
        self.assertGreaterEqual(len(name), server.NAME_WORD_MIN)
        self.assertNotIn(name.casefold(), server.GENERIC_USERNAMES)
        with mock.patch.dict(os.environ, {"USERNAME": name, "USER": name}, clear=False):
            with mock.patch("getpass.getuser", return_value=name):
                out = server.scrub_tail("user=xyz xyza xyz_x")
        self.assertIn("user=<user>", out)
        self.assertIn("xyza", out)
        self.assertIn("xyz_x", out)
        self.assertNotIn("user=xyz", out)

    def test_generic_usernames_path_segment_only(self):
        self.assertEqual(
            server.GENERIC_USERNAMES,
            frozenset({"test", "user", "admin", "administrator", "bin", "guest", "owner"}),
        )
        for name in ("test", "user", "admin", "administrator", "bin", "guest", "owner"):
            with mock.patch.dict(os.environ, {"USERNAME": name, "USER": name}, clear=False):
                with mock.patch("getpass.getuser", return_value=name):
                    word = server.scrub_tail("hello %s world" % name)
            self.assertNotIn("<user>", word)
            if len(name) < 6:
                self.assertIn("hello %s world" % name, word)
            with mock.patch.dict(os.environ, {"USERNAME": name, "USER": name}, clear=False):
                with mock.patch("getpass.getuser", return_value=name):
                    path = server.scrub_tail(r"C:\Users\%s\secret" % name)
            self.assertNotIn("\\%s\\" % name, path)
        with mock.patch.dict(os.environ, {"USERNAME": "bin", "USER": "bin"}, clear=False):
            with mock.patch("getpass.getuser", return_value="bin"):
                git = server.scrub_tail(r"see C:\Git\bin\git.exe extra")
                posix = server.scrub_tail("see /usr/bin/env extra")
        self.assertIn(r"\Git\bin\git.exe", git)
        self.assertNotIn("<user>", git)
        self.assertIn("/usr/bin/env", posix)
        self.assertNotIn("<user>", posix)

    def test_username_left_word_boundary(self):
        name = "xyz"
        with mock.patch.dict(os.environ, {"USERNAME": name, "USER": name}, clear=False):
            with mock.patch("getpass.getuser", return_value=name):
                out = server.scrub_tail("axyz xyz zxyz")
        self.assertIn("axyz", out)
        self.assertIn("zxyz", out)
        self.assertIn("<user>", out)
        self.assertNotIn(" xyz ", " %s " % out)

    def test_generic_list_uses_casefold(self):
        self.assertIn("bin", server.GENERIC_USERNAMES)
        self.assertNotIn("BIN", server.GENERIC_USERNAMES)
        self.assertFalse(server._username_word_ok("BIN"))
        self.assertFalse(server._username_word_ok("Admin"))
        self.assertFalse(server._username_word_ok("TEST"))
        with mock.patch.dict(os.environ, {"USERNAME": "BIN", "USER": "BIN"}, clear=False):
            with mock.patch("getpass.getuser", return_value="BIN"):
                word = server.scrub_tail("hello BIN world")
                git = server.scrub_tail(r"C:\Git\BIN\git.exe")
                users = server.scrub_tail(r"C:\Users\BIN\secret")
        self.assertIn("hello BIN world", word)
        self.assertNotIn("<user>", word)
        self.assertIn(r"\Git\BIN\git.exe", git)
        self.assertNotIn("\\BIN\\", users)

    def test_four_char_username_in_probe_user(self):
        name = "wxyz"
        self.assertEqual(len(name), 4)
        self.assertGreaterEqual(len(name), server.NAME_WORD_MIN)
        with mock.patch.dict(os.environ, {"USERNAME": name, "USER": name}, clear=False):
            with mock.patch("getpass.getuser", return_value=name):
                out = server.scrub_tail("probe-user=" + name)
        self.assertIn("probe-user=<user>", out)
        self.assertNotIn(name, out)

    def test_scrub_is_idempotent(self):
        raws = [
            "boom at " + WIN_PATH + " user=" + USER,
            "HEAD-UNIQUE\n" + "\n".join("mid-%s" % index for index in range(80)) + "\nTAIL-UNIQUE",
            "HEAD-UNIQUE" + ("x" * 5000) + "TAIL-UNIQUE",
            "short ok",
            "user=e the <home> scene",
            "user=home dir=<home> " + POSIX_PATH,
        ]
        env = {"USERNAME": USER, "USER": USER, "USERPROFILE": PROFILE, ENV_KEY: ENV_VAL}
        with mock.patch.dict(os.environ, env, clear=False):
            for raw in raws:
                once = server.scrub_tail(raw)
                twice = server.scrub_tail(once)
                self.assertEqual(once, twice, raw)
                self.assertLessEqual(once.count("<truncated>"), 1, raw)
                self.assertEqual(once.count("<truncated>"), twice.count("<truncated>"), raw)
                self.assertEqual(len(once.splitlines()), len(twice.splitlines()), raw)

    def test_users_dir_before_profile_parent(self):
        profile = r"C:\Users\agentid"
        other = r"C:\Users\otherperson\x"
        with mock.patch.dict(os.environ, {"USERPROFILE": profile, "USERNAME": USER, "USER": USER}, clear=False):
            out = server.scrub_tail("see " + other)
        self.assertNotIn("otherperson", out)
        self.assertNotIn(r"<home>\otherperson", out)
        self.assertIn("<home>", out)

    def test_harmless_env_keys_survive(self):
        extra = {
            "OS": "Windows_NT",
            "LANG": "en_US.UTF-8",
            "NUMBER_OF_PROCESSORS": "16",
            "PROCESSOR_ARCHITECTURE": "AMD64",
            "PROCESSOR_IDENTIFIER": "Intel64 Family 6",
        }
        with mock.patch.dict(os.environ, extra, clear=False):
            out = server.scrub_tail(
                "OS=Windows_NT LANG=en_US.UTF-8 nproc=16 arch=AMD64 id=Intel64 Family 6"
            )
        self.assertIn("Windows_NT", out)
        self.assertIn("en_US.UTF-8", out)
        self.assertIn("Intel64 Family 6", out)

    def test_secret_under_skip_or_prefix_key_is_scrubbed(self):
        secret = "abcdef123456secret"  # gitleaks:allow — fake fixture, not a credential
        with mock.patch.dict(os.environ, {"LANG": secret, "PROCESSOR_FAKE": secret}, clear=False):
            out = server.scrub_tail("see " + secret)
        self.assertNotIn(secret, out)
        self.assertIn("<env>", out)

    def test_run_impl_stdout_is_scrubbed(self):
        tmp = tempfile.TemporaryDirectory()
        key = bytearray(os.urandom(32))
        prev_bus, prev_log, prev_seen, prev_root = server.BUS, server.LOG, server.SEEN, server.ROOT
        try:
            home = Path(tmp.name)
            bus = home / "bus"
            bus.mkdir()
            for name in ("LEAD-TO-BUILD.md", "BUILD-TO-LEAD.md"):
                (bus / name).write_text("", encoding="utf-8")
            server.BUS, server.LOG, server.SEEN, server.ROOT = bus, home / "approvals.jsonl", home / ".seen-ids.txt", home
            server.set_run_impl(lambda pane, command: (1, "boom at " + WIN_PATH, "stderr-ok"))
            sigverify.set_key_provider(lambda owner: bytearray(key) if owner == "Lead" else None)
            signed = _sign(_job(id="scrub-impl-1"), key)
            code, out, err = server.run_pane("1", signed["job"], signed, "Lead")
            self.assertEqual(code, 1)
            self.assertNotIn("Users\\agentid", out)
            self.assertIn("<home>", out)
            self.assertEqual(err, "stderr-ok")
        finally:
            server.set_run_impl(None)
            sigverify.clear_key_provider()
            sigverify._wipe(key)
            server.BUS, server.LOG, server.SEEN, server.ROOT = prev_bus, prev_log, prev_seen, prev_root
            tmp.cleanup()

    def test_apply_decision_rescrubs_run_pane_result(self):
        tmp = tempfile.TemporaryDirectory()
        key = bytearray(os.urandom(32))
        prev_bus, prev_log, prev_seen, prev_root = server.BUS, server.LOG, server.SEEN, server.ROOT
        prev_sessions = server.SESSIONS
        prev_events = getattr(server, "EVENTS", None)
        prev_broken = getattr(server, "_sessions_broken", False)
        try:
            home = Path(tmp.name)
            bus = home / "bus"
            bus.mkdir()
            for name in ("LEAD-TO-BUILD.md", "BUILD-TO-LEAD.md"):
                (bus / name).write_text("", encoding="utf-8")
            server.BUS, server.LOG, server.SEEN, server.ROOT = bus, home / "approvals.jsonl", home / ".seen-ids.txt", home
            server.SESSIONS = home / ".sessions.json"
            if prev_events is not None:
                server.EVENTS = home / ".hub-events.jsonl"
            server._sessions_broken = False
            protocol.set_approvals_log(server.LOG)
            protocol.read_seen(server.SEEN)
            sigverify.set_key_provider(lambda owner: bytearray(key) if owner == "Lead" else None)
            _write_signed(bus, _job(id="scrub-re-1"), key)
            server.set_run_impl(lambda pane, command: (1, "boom at " + WIN_PATH, ""))
            status, payload = server.apply_decision("scrub-re-1", "approve")
            self.assertEqual(status, 200)
            self.assertNotIn("Users\\agentid", payload["result"])
            self.assertIn("<home>", payload["result"])
        finally:
            server.set_run_impl(None)
            sigverify.clear_key_provider()
            sigverify._wipe(key)
            server.BUS, server.LOG, server.SEEN, server.ROOT = prev_bus, prev_log, prev_seen, prev_root
            server.SESSIONS = prev_sessions
            if prev_events is not None:
                server.EVENTS = prev_events
            server._sessions_broken = prev_broken
            tmp.cleanup()

    def test_home_replaced_before_username(self):
        with mock.patch.dict(os.environ, {"USERPROFILE": PROFILE, "USERNAME": USER, "USER": USER}, clear=False):
            with mock.patch("getpass.getuser", return_value=USER):
                out = server.scrub_tail(r"X:\Profiles\agentid\secret")
        self.assertIn("<home>", out)
        self.assertNotIn(r"X:\Profiles", out)
        self.assertNotIn("<user>", out)

    def test_regex_sees_only_pre_capped_tail(self):
        seen = []
        real = server._USERS_DIR

        class Spy:
            def sub(self, repl, string):
                seen.append(len(string))
                return real.sub(repl, string)

        with mock.patch.object(server, "_USERS_DIR", Spy()):
            server.scrub_tail("HEAD-SECRET" + ("/" * 20000) + r"C:\Users\agentid\tail")
        self.assertTrue(seen)
        self.assertLessEqual(max(seen), server.SCRUB_PRE_CAP)

    def test_name_pattern_is_bounded(self):
        self.assertEqual(server._NAME, r"[^\s\\/%]{1,64}")
        self.assertNotIn("+", server._NAME)
        self.assertNotIn("*", server._NAME)

    def test_percent5c_home_is_scrubbed(self):
        with mock.patch.dict(os.environ, {"USERPROFILE": PROFILE, "USERNAME": USER, "USER": USER}, clear=False):
            out = server.scrub_tail("see x:%5Cprofiles%5Cagentid%5Cfile")
        self.assertNotIn("agentid", out.lower())
        self.assertIn("<home>", out)

    def test_json_escaped_home_is_scrubbed(self):
        with mock.patch.dict(os.environ, {"USERPROFILE": PROFILE, "USERNAME": USER, "USER": USER}, clear=False):
            out = server.scrub_tail(r"see X:\\Profiles\\agentid\\file")
        self.assertNotIn("agentid", out.lower())
        self.assertIn("<home>", out)

    def test_huge_truncated_input_is_still_capped(self):
        raw = "LEAK-HEAD\n" + ("x" * (500 * 1024)) + "\n<truncated>"
        out = server.scrub_tail(raw)
        self.assertLessEqual(len(out), server.TAIL_MAX_CHARS + len(server.TRUNC_MARK))
        self.assertNotIn("LEAK-HEAD", out)
        self.assertTrue(out.endswith(server.TRUNC_MARK))
        self.assertEqual(out.count("<truncated>"), 1)
        self.assertEqual(server.scrub_tail(out), out)

    def test_angle_username_is_scrubbed(self):
        with mock.patch.dict(os.environ, {"USERNAME": USER, "USER": USER}, clear=False):
            with mock.patch("getpass.getuser", return_value=USER):
                out = server.scrub_tail("see <agentid> here")
        self.assertNotIn("agentid", out)
        self.assertIn("<user>", out)

    def test_angle_env_secret_is_scrubbed(self):
        secret = "<sk-abcdef123456>"
        with mock.patch.dict(os.environ, {"OS": secret, ENV_KEY: secret}, clear=False):
            out = server.scrub_tail("token " + secret + " leaked")
        self.assertNotIn("sk-abcdef123456", out)
        self.assertNotIn(secret, out)
        self.assertIn("<env>", out)

    def test_literal_hbcph0end_is_left_alone(self):
        out = server.scrub_tail("marker HBCPH0END stays")
        self.assertIn("HBCPH0END", out)

    def test_username_match_is_case_insensitive(self):
        with mock.patch.dict(os.environ, {"USERNAME": "Agentid", "USER": "Agentid"}, clear=False):
            with mock.patch("getpass.getuser", return_value="Agentid"):
                out = server.scrub_tail("user=agentid failed")
        self.assertNotIn("agentid", out.lower())
        self.assertIn("<user>", out)

    def test_percent5c_only_variant_is_scrubbed(self):
        variants = server._path_variants(PROFILE)
        self.assertIn("X:%5CProfiles%5Cagentid", variants)
        with mock.patch.dict(os.environ, {"USERPROFILE": PROFILE, "USERNAME": USER, "USER": USER}, clear=False):
            out = server.scrub_tail("see X:%5CProfiles%5Cagentid%5Csecret")
        self.assertNotIn("agentid", out.lower())
        self.assertNotIn("%5CProfiles%5C", out)
        self.assertIn("<home>", out)

    def test_scrub_probe_home_user_tail_and_truncated(self):
        """Build staging: <hub python> -m unittest tests.test_scrub_tail -k probe"""
        script = ROOT / "tools" / "scrub-probe.py"
        self.assertTrue(script.is_file())
        tmp = tempfile.TemporaryDirectory()
        try:
            fake_home = Path(tmp.name) / "agentid"
            fake_home.mkdir()
            env = dict(os.environ)
            env["HOME"] = str(fake_home)
            env["USERPROFILE"] = str(fake_home)
            env["USERNAME"] = USER
            env["USER"] = USER
            completed = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(completed.returncode, 1)
            raw_lines = completed.stdout.splitlines()
            self.assertGreater(len(raw_lines), 3000)
            self.assertEqual(raw_lines[0], "probe-line-0001")
            self.assertEqual(raw_lines[-1], "probe-line-3200")
            self.assertIn(str(fake_home), completed.stderr)
            self.assertIn(USER, completed.stderr)

            with mock.patch.dict(os.environ, {
                "HOME": str(fake_home),
                "USERPROFILE": str(fake_home),
                "USERNAME": USER,
                "USER": USER,
            }, clear=False):
                out = server.scrub_tail(completed.stdout)
                err = server.scrub_tail(completed.stderr)

            expected_tail = "\n".join("probe-line-%04d" % index for index in range(3161, 3201))
            self.assertEqual(out, expected_tail + server.TRUNC_MARK)
            self.assertIn("<truncated>", out)
            self.assertNotIn("probe-line-0001", out)
            self.assertNotIn("probe-line-3160", out)
            self.assertIn("probe-line-3161", out)
            self.assertIn("probe-line-3200", out)
            self.assertLessEqual(len(out.splitlines()), server.TAIL_MAX_LINES + 1)
            self.assertNotIn(str(fake_home), out)
            self.assertNotIn(str(fake_home), err)
            self.assertNotIn(USER, err)
            self.assertIn("probe-home=<home>", err)
            self.assertIn("probe-user=<user>", err)
        finally:
            tmp.cleanup()

    def test_scrub_probe_stderr_through_run_pane(self):
        tmp = tempfile.TemporaryDirectory()
        key = bytearray(os.urandom(32))
        prev_grok = server.GROK
        prev_bus, prev_log, prev_seen, prev_root = server.BUS, server.LOG, server.SEEN, server.ROOT
        try:
            home = Path(tmp.name)
            fake_home = home / "agentid"
            fake_home.mkdir()
            bus = home / "bus"
            bus.mkdir()
            for name in ("LEAD-TO-BUILD.md", "BUILD-TO-LEAD.md"):
                (bus / name).write_text("", encoding="utf-8")
            grok = _write_fake_grok(
                home,
                "import os, subprocess, sys\n"
                "env = dict(os.environ)\n"
                "env['HOME'] = %r\n"
                "env['USERPROFILE'] = %r\n"
                "env['USERNAME'] = %r\n"
                "env['USER'] = %r\n"
                "raise SystemExit(subprocess.call([sys.executable, %r], env=env))\n"
                % (
                    str(fake_home),
                    str(fake_home),
                    USER,
                    USER,
                    str(ROOT / "tools" / "scrub-probe.py"),
                ),
            )
            server.GROK = grok
            server.BUS = bus
            server.LOG = home / "approvals.jsonl"
            server.SEEN = home / ".seen-ids.txt"
            server.ROOT = home
            server.set_run_impl(None)
            server.set_test_grok_argv(True)
            signed = _sign(_job(id="scrub-probe-run-1"), key)
            sigverify.set_key_provider(lambda owner: bytearray(key) if owner == "Lead" else None)
            with mock.patch.dict(os.environ, {
                "HOME": str(fake_home),
                "USERPROFILE": str(fake_home),
                "USERNAME": USER,
                "USER": USER,
            }, clear=False):
                code, out, err = server.run_pane("1", signed["job"], signed, "Lead")
            self.assertEqual(code, 1)
            self.assertIn("probe-home=<home>", err)
            self.assertIn("probe-user=<user>", err)
            self.assertNotIn(str(fake_home), err)
            self.assertNotIn(USER, err)
            self.assertIn("<truncated>", out)
            self.assertNotIn(str(fake_home), out)
        finally:
            server.set_run_impl(None)
            server.set_test_grok_argv(False)
            server.GROK = prev_grok
            sigverify.clear_key_provider()
            sigverify._wipe(key)
            server.BUS, server.LOG, server.SEEN, server.ROOT = prev_bus, prev_log, prev_seen, prev_root
            tmp.cleanup()

    def test_grok_argv_windows_fakes(self):
        prev = server.GROK
        try:
            with mock.patch.object(os, "name", "nt"):
                server.GROK = Path(r"X:\bin\grok.exe")
                self.assertEqual(server._grok_argv("-p", "job"), [r"X:\bin\grok.exe", "-p", "job"])
                server.GROK = Path(r"X:\tmp\fake-grok.cmd")
                self.assertEqual(
                    server._grok_argv("-p", "job"),
                    [r"X:\tmp\fake-grok.cmd", "-p", "job"],
                )
                server.set_test_grok_argv(True)
                self.assertEqual(server._grok_argv("-p", "job")[0].lower().endswith("cmd.exe"), True)
                argv = server._grok_argv("-p", "job")
                self.assertEqual(argv[1], "/c")
                self.assertEqual(argv[2], r"X:\tmp\fake-grok.cmd")
                server.GROK = Path(r"X:\tmp\fake-grok.py")
                argv = server._grok_argv("-p", "job")
                self.assertEqual(argv[0], sys.executable)
                self.assertEqual(argv[1], r"X:\tmp\fake-grok.py")
        finally:
            server.set_test_grok_argv(False)
            server.GROK = prev

    def test_probe_job_uses_hub_python_not_py_launcher(self):
        example = (ROOT / "tools" / "scrub-probe-job.example.md").read_text(encoding="utf-8")
        testers = (ROOT / "TESTERS.md").read_text(encoding="utf-8")
        e2e = (ROOT / "tools" / "scrub_e2e.py").read_text(encoding="utf-8")
        for blob in (example, testers):
            self.assertIn("<hub python> -m tools.scrub_e2e", blob)
            self.assertNotIn("py -3", blob)
            self.assertIn("probe-line-3161", blob)
            self.assertIn("probe-line-3200", blob)
            self.assertIn("<truncated>", blob)
            self.assertIn("probe-home=<home>", blob)
            self.assertIn("probe-user=<user>", blob)
            self.assertNotIn(sys.executable, blob)
        self.assertIn("<hub python> -m tools.scrub_e2e", e2e)
        self.assertNotIn("py -3", e2e)
        self.assertIn("[sys.executable, str(PROBE)]", e2e)
        self.assertNotIn("python.exe", example.lower())
        self.assertNotIn("python3", example.lower())
        lowered = example.lower()
        self.assertNotIn("users\\", lowered)
        self.assertNotIn("appdata", lowered)
        self.assertIn("BAD_PANE", example)
        self.assertIn("temp state folder", example)
        self.assertIn("Panes do not need", example)

    def test_scrub_e2e_cli_pass(self):
        completed = subprocess.run(
            [sys.executable, "-m", "tools.scrub_e2e"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + "\n" + completed.stderr)
        for name in (
            "probe-line-3161 to 3200",
            "<truncated>",
            "<home>",
            "<user>",
            "no raw home or username",
        ):
            self.assertIn("PASS " + name, completed.stdout)
            self.assertNotIn("FAIL " + name, completed.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
