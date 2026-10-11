"""Staging hubs never auto-resume the live watcher or share the live handoff."""

import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server
from tests.live_guard import install_live_state_guard, point_hub_at, restore_hub

install_live_state_guard()


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


class FakeSpawner:
    def __init__(self):
        self.calls = []

    def __call__(self, pane, env, cwd):
        pty = FakePty()
        self.calls.append({"pane": pane, "env": env, "cwd": cwd, "pty": pty})
        return pty


class TestStagingIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "handoff").mkdir()
        self.live = self.home / "live-handoff"
        self.live.mkdir()
        self.bus = self.home / "bus"
        self.bus.mkdir()
        for name in (
            "LEAD-TO-BUILD.md",
            "GROKBOT-TO-BUILD.md",
            "BUILD-TO-LEAD.md",
            "BUILD-TO-GROKBOT.md",
        ):
            (self.bus / name).write_text("", encoding="utf-8")
        self.prev = point_hub_at(self.home)
        self.prev_handoff = server.HANDOFF
        self.prev_panes = dict(server.PANES)
        self.prev_bus = server.BUS
        self.prev_log = server.LOG
        self.prev_seen = server.SEEN
        self.prev_sessions = server.SESSIONS
        self.prev_events = server.EVENTS
        server.BUS = self.bus
        server.LOG = self.home / "approvals.jsonl"
        server.SEEN = self.home / ".seen-ids.txt"
        server.SESSIONS = self.home / ".sessions.json"
        server.EVENTS = self.home / ".hub-events.jsonl"
        server.PANES.clear()
        self.spawner = FakeSpawner()
        server.set_spawn_impl(self.spawner)

    def tearDown(self):
        server.set_spawn_impl(None)
        server.PANES.clear()
        server.PANES.update(self.prev_panes)
        server.BUS = self.prev_bus
        server.LOG = self.prev_log
        server.SEEN = self.prev_seen
        server.SESSIONS = self.prev_sessions
        server.EVENTS = self.prev_events
        server.HANDOFF = self.prev_handoff
        restore_hub(self.prev)
        self.tmp.cleanup()

    def _mark(self):
        (self.home / ".hbc-staging").write_text("", encoding="utf-8")

    def _staging_env(self, **extra):
        env = {"HBC_STAGING": "1"}
        env.update(extra)
        return env

    def test_is_staging_requires_flag_and_marker_port_never_decides(self):
        self.assertFalse(server.is_staging(environ={}, root=self.home, port=8107))
        self.assertFalse(server.is_staging(environ={"HBC_HUB_PORT": "8117"}, root=self.home, port=8107))
        self.assertFalse(server.is_staging(environ={}, root=self.home, port=8117))
        self.assertFalse(server.is_staging(environ={"HBC_STAGING": "1"}, root=self.home, port=8107))
        self._mark()
        self.assertFalse(server.is_staging(environ={}, root=self.home, port=8107))
        self.assertFalse(server.is_staging(environ={"HBC_HUB_PORT": "8117"}, root=self.home, port=8117))
        self.assertTrue(server.is_staging(environ={"HBC_STAGING": "1"}, root=self.home, port=8107))
        self.assertTrue(server.is_staging(environ=self._staging_env(HBC_HUB_PORT="8107"), root=self.home))
        src = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn("def is_staging(", src)
        body = src.split("def is_staging(", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("HBC_STAGING", body)
        self.assertIn(".hbc-staging", body)
        self.assertIn("Port never decides", body)
        self.assertNotIn("HBC_HUB_PORT", body)
        self.assertNotIn("DEFAULT_HUB_PORT", body)
        start = src.split("async def start_hub", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("warn_or_refuse_staging()", start)
        self.assertIn("is_staging()", start)
        self.assertIn("log_hub_event(\"staging\"", start)
        self.assertLess(start.find("warn_or_refuse_staging()"), start.find("configure_handoff()"))
        self.assertLess(start.find("is_staging()"), start.find("configure_handoff()"))
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Live uses the default port 8107", readme)
        self.assertIn("HBC_STAGING=1", readme)

    def test_marker_only_is_live_safe_flag_only_refuses_start(self):
        self._mark()
        logs = io.StringIO()
        with patch("sys.stdout", logs):
            server.warn_or_refuse_staging(environ={}, root=self.home)
        self.assertIn("WARNING", logs.getvalue())
        self.assertIn("treating as LIVE", logs.getvalue())
        self.assertFalse(server.is_staging(environ={}, root=self.home))
        with patch.object(server, "live_handoff_dir", return_value=self.live):
            configured = server.configure_handoff(environ={}, root=self.home)
            self.assertIsNotNone(configured)
            self.assertNotEqual(Path(configured).resolve(), (self.home / "handoff-staging").resolve())
        (self.home / ".hbc-staging").unlink()
        with self.assertRaises(SystemExit) as raised:
            server.warn_or_refuse_staging(environ={"HBC_STAGING": "1"}, root=self.home)
        self.assertIn(".hbc-staging", str(raised.exception))
        logs2 = io.StringIO()
        with patch("sys.stdout", logs2), self.assertRaises(SystemExit):
            server.warn_or_refuse_staging(environ={"HBC_STAGING": "1"}, root=self.home)
        self.assertIn("WARNING", logs2.getvalue())
        self.assertIn("refuse to start", logs2.getvalue())

        from aiohttp import web

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
                raise AssertionError("hub must not bind when HBC_STAGING=1 lacks the marker")

        import asyncio

        with patch.object(web, "AppRunner", Runner), patch.object(web, "TCPSite", Site), patch.dict(
            os.environ, {"HBC_STAGING": "1"}, clear=False
        ), self.assertRaises(SystemExit):
            asyncio.run(server.start_hub())

    def test_staging_skips_autoreopen_on_flag_and_marker(self):
        server.ensure_pane("build")
        server.PANES.clear()
        self.assertEqual(len(self.spawner.calls), 1)
        port_only = {"HBC_BUILD_AUTOREOPEN": "1", "HBC_HUB_PORT": "8117"}
        server.reopen_build_sessions(environ=port_only)
        self.assertEqual(len(self.spawner.calls), 2)
        self._mark()
        server.PANES.clear()
        marker_only = {"HBC_BUILD_AUTOREOPEN": "1"}
        server.reopen_build_sessions(environ=marker_only)
        self.assertEqual(len(self.spawner.calls), 3)
        on = self._staging_env(HBC_BUILD_AUTOREOPEN="1")
        server.PANES.clear()
        before = len(self.spawner.calls)
        server.reopen_build_sessions(environ=on)
        self.assertEqual(len(self.spawner.calls), before)
        self.assertEqual(server.pane_boot_command("build", environ=on), "")
        self.assertNotIn("--continue", server.pane_boot_command("build", environ=on))
        self.assertNotIn(server.WATCHER_PROMPT, server.pane_boot_command("build", environ=on))

    def test_staging_handoff_defaults_off_live_and_refuses_live_path(self):
        self._mark()
        env = self._staging_env()
        with patch.object(server, "live_handoff_dir", return_value=self.live):
            configured = server.configure_handoff(environ=env, root=self.home)
            self.assertIsNotNone(configured)
            self.assertEqual(Path(configured).resolve(), (self.home / "handoff-staging").resolve())
            self.assertFalse(server.is_live_handoff_path(configured))
            self.assertIsNone(server.configure_handoff(str(self.live), environ=env, root=self.home))
            self.assertIsNone(server.HANDOFF)
            other = self.home / "other-handoff"
            other.mkdir()
            ok = server.configure_handoff(str(other), environ=env, root=self.home)
            self.assertEqual(Path(ok).resolve(), other.resolve())

    def test_staging_ignores_inherited_handoff_outside_root(self):
        self._mark()
        outside = Path(tempfile.mkdtemp(prefix="hbc-hand-"))
        try:
            inherited = self._staging_env(HBC_HANDOFF=str(outside))
            with patch.object(server, "live_handoff_dir", return_value=self.live):
                configured = server.configure_handoff(environ=inherited, root=self.home)
                self.assertIsNotNone(configured)
                self.assertEqual(Path(configured).resolve(), (self.home / "handoff-staging").resolve())
            inside = self.home / "handoff-lab"
            inside.mkdir()
            under = self._staging_env(HBC_HANDOFF=str(inside))
            ok = server.configure_handoff(environ=under, root=self.home)
            self.assertEqual(Path(ok).resolve(), inside.resolve())
            default_live = server.default_live_handoff_dir()
            refuse_default = self._staging_env(HBC_HANDOFF=str(default_live))
            with patch.object(server, "live_handoff_dir", return_value=self.live):
                self.assertIsNone(server.configure_handoff(str(default_live), environ=refuse_default, root=self.home))
                self.assertIsNone(server.HANDOFF)
                self.assertIsNone(server.configure_handoff(str(self.live), environ=refuse_default, root=self.home))
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_watcher_refused_when_staging_handoff_equals_live(self):
        server.HANDOFF = self.live
        self._mark()
        env = self._staging_env()
        with patch.object(server, "live_handoff_dir", return_value=self.live):
            self.assertFalse(server.watcher_boot_allowed(environ=env, root=self.home))
            self.assertEqual(server.pane_boot_command("build", environ=env), "")
            self.assertTrue(server.is_staging(environ=env, root=self.home))

    def test_is_live_handoff_path_junction_and_case(self):
        with patch.object(server, "live_handoff_dir", return_value=self.live):
            self.assertTrue(server.is_live_handoff_path(self.live))
            alias = self.home / "alias-live"
            linked = False
            try:
                alias.symlink_to(self.live, target_is_directory=True)
                linked = alias.exists()
            except (OSError, NotImplementedError):
                linked = False
            if linked:
                self.assertTrue(server.is_live_handoff_path(alias))
            mixed = Path(str(self.live).swapcase())
            if os.path.normcase("A") == os.path.normcase("a"):
                self.assertTrue(server.is_live_handoff_path(mixed))
            src = Path(server.__file__).read_text(encoding="utf-8")
            live_fn = src.split("def is_live_handoff_path", 1)[1].split("\ndef ", 1)[0]
            self.assertIn("normcase", src)
            self.assertIn("resolve()", src)
            self.assertIn("default_live_handoff_dir", live_fn)
            self.assertIn("live_handoff_dir", live_fn)

    def test_claim_refuses_live_handoff_jobs_on_staging(self):
        fields = {"id": "live-1", "sig": "a" * 64}
        with patch.object(server, "is_staging", return_value=True), patch.object(
            server, "is_live_handoff_path", return_value=True
        ), patch.object(server, "handoff_dir", return_value=self.live), patch.object(
            server, "find_job", return_value=(fields, "Lead", server.SOURCE_HANDOFF)
        ):
            self.assertEqual(server.claim_job("live-1", "a" * 64), "REFUSED LIVE_HANDOFF")
        with patch.object(server, "is_staging", return_value=False), patch.object(
            server, "is_live_handoff_path", return_value=True
        ), patch.object(server, "handoff_dir", return_value=self.live), patch.object(
            server, "find_job", return_value=(fields, "Lead", server.SOURCE_HANDOFF)
        ), patch.object(server, "routes_to_watcher", return_value=False):
            self.assertNotEqual(server.claim_job("live-1", "a" * 64), "REFUSED LIVE_HANDOFF")
        src = Path(server.__file__).read_text(encoding="utf-8")
        claim = src.split("def claim_job", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("REFUSED LIVE_HANDOFF", claim)
        self.assertIn("is_staging()", claim)
        self.assertIn("is_live_handoff_path", claim)

    def test_claim_helper_refuses_live_handoff_on_staging_hub(self):
        script = (ROOT / "tools" / "Claim-BusJob.ps1").read_text(encoding="utf-8")
        self.assertIn("function Test-StagingHub", script)
        self.assertIn("function Test-LiveHandoffFile", script)
        self.assertIn("REFUSED LIVE_HANDOFF", script)
        self.assertIn("$DefaultHubPort", script)
        self.assertIn("HBC_STAGING", script)
        self.assertIn(".hbc-staging", script)
        self.assertIn(".grok", script)
        self.assertIn("handoff", script)
        self.assertIn("$userProfile", script)
        self.assertNotIn("$home =", script)
        self.assertIn("Test-StagingHub $Hub", script)
        self.assertIn("Test-LiveHandoffFile $File", script)
        staging_fn = script.split("function Test-StagingHub", 1)[1].split("function ", 1)[0]
        self.assertNotIn("$port -ne $DefaultHubPort", staging_fn)
        self.assertIn("HBC_STAGING", staging_fn)
        self.assertLess(
            script.find("REFUSED LIVE_HANDOFF"),
            script.find("$uri = $Hub.TrimEnd(\"/\") + \"/api/claim\""),
        )
        dropped = script.replace("REFUSED LIVE_HANDOFF", "REFUSED ERROR")
        self.assertNotIn("REFUSED LIVE_HANDOFF", dropped)

    def _run_claim_helper(self, env, job_id):
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if not shell:
            self.skipTest("Claim-BusJob LIVE_HANDOFF: powershell.exe (5.1) or pwsh not on PATH")
        work = Path(tempfile.mkdtemp(prefix="hbc-claim-"))
        try:
            tools = work / "tools"
            tools.mkdir()
            shutil.copy2(ROOT / "tools" / "Claim-BusJob.ps1", tools / "Claim-BusJob.ps1")
            (work / ".hbc-staging").write_text("", encoding="utf-8")
            live_file = Path.home() / ".grok" / "handoff" / "LEAD-TO-BUILD.md"
            merged = os.environ.copy()
            merged.pop("HBC_STAGING", None)
            merged.update(env)
            completed = subprocess.run(
                [
                    shell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(tools / "Claim-BusJob.ps1"),
                    "-Id",
                    job_id,
                    "-File",
                    str(live_file),
                    "-Sig",
                    "ab" * 32,
                ],
                capture_output=True,
                text=True,
                timeout=20,
                env=merged,
            )
            return completed
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_claim_helper_runtime_refuses_live_handoff(self):
        completed = self._run_claim_helper({"HBC_STAGING": "1"}, "live-hand-1")
        out = (completed.stdout or "") + (completed.stderr or "")
        lines = [ln.strip() for ln in (completed.stdout or "").splitlines() if ln.strip()]
        self.assertIn("REFUSED LIVE_HANDOFF", lines, out)
        self.assertNotIn("REFUSED", lines)
        self.assertEqual(completed.returncode, 2, out)
        script = (ROOT / "tools" / "Claim-BusJob.ps1").read_text(encoding="utf-8")
        fail = script.split("function Write-FailClosed", 1)[1].split("try {", 1)[0]
        self.assertIn("[Console]::Out.WriteLine($verdict)", fail)
        self.assertNotIn("Write-Output $verdict", fail)

    def test_claim_helper_runtime_marker_only_is_live(self):
        completed = self._run_claim_helper({}, "live-hand-2")
        out = (completed.stdout or "") + (completed.stderr or "")
        self.assertNotIn("REFUSED LIVE_HANDOFF", out)
        self.assertNotEqual(completed.returncode, 2)

    def test_staging_exposed_and_ensure_pane_does_not_spawn(self):
        live_root = Path(server.__file__).resolve().parent
        live_seen = live_root / ".seen-ids.txt"
        live_init = live_root / ".seen-ids.txt.init"
        seen_stat = live_seen.stat() if live_seen.is_file() else None
        init_existed = live_init.exists()
        with patch.object(server, "is_staging", return_value=True):
            self.assertTrue(server.state_payload().get("staging"))
            self.assertFalse(server.ensure_pane("build"))
            self.assertFalse(server.ensure_pane("build2"))
            self.assertEqual(self.spawner.calls, [])
            self.assertEqual(server.PANES, {})
        if seen_stat is None:
            self.assertFalse(live_seen.is_file())
        else:
            self.assertEqual(live_seen.stat().st_mtime_ns, seen_stat.st_mtime_ns)
        self.assertEqual(live_init.exists(), init_existed)
        self.assertNotEqual(Path(server.SEEN).resolve(), live_seen.resolve())
        self.assertNotEqual(Path(server.BUS).resolve(), (live_root / "bus").resolve())
        self.assertNotEqual(Path(server.LOG).resolve(), (live_root / "approvals.jsonl").resolve())
        src = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn('"staging": is_staging()', src)
        start = src.split("def ensure_pane", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("is_staging()", start)
        self.assertLess(start.find("is_staging()"), start.find("spawn_pane"))
        restart = src.split("async def restart_build", 1)[1].split("\nasync def ", 1)[0]
        self.assertIn("is_staging()", restart)
        self.assertIn('"error": "staging"', restart)

    def test_start_hub_inherits_staging_env_and_refuses_quickly(self):
        form = (ROOT / "shell" / "Form1.cs").read_text(encoding="utf-8")
        spawn = form.split("private static void ApplyHubSpawnEnv", 1)[1].split("\n    private ", 1)[0]
        self.assertIn("HBC_STAGING", spawn)
        self.assertIn("HBC_HUB_PORT", spawn)
        self.assertIn('staging != "1"', spawn)
        self.assertIn("EnvironmentVariables", spawn)
        start = form.split("private async Task StartHub()", 1)[1].split("\n    private ", 1)[0]
        self.assertIn("ApplyHubSpawnEnv(psi)", start)
        self.assertIn("WaitForExitAsync", start)
        self.assertIn("hub start refused", start)
        self.assertNotIn(".Wait()", start)
        self.assertNotIn("GetResult()", start)
        self.assertNotIn("Thread.Sleep", start)
        self.assertLess(start.find("ExitCode != 0"), start.find("WaitForHubAsync"))
        starter = (ROOT / "Start-HiveBrainComms.ps1").read_text(encoding="utf-8")
        self.assertIn("HBC_STAGING", starter)
        self.assertIn(".hbc-staging", starter)
        self.assertLess(starter.find("refuse to start"), starter.find("while (-not (Test-Up)"))
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if not shell:
            self.skipTest("Start-HiveBrainComms refuse: powershell.exe (5.1) or pwsh not on PATH")
        work = Path(tempfile.mkdtemp(prefix="hbc-start-"))
        try:
            script = work / "Start-HiveBrainComms.ps1"
            script.write_text(starter, encoding="utf-8")
            env = os.environ.copy()
            env["HBC_STAGING"] = "1"
            env["HBC_HUB_PORT"] = "8117"
            started = time.monotonic()
            completed = subprocess.run(
                [
                    shell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                ],
                capture_output=True,
                text=True,
                timeout=8,
                env=env,
                cwd=str(work),
            )
            elapsed = time.monotonic() - started
            out = (completed.stdout or "") + (completed.stderr or "")
            self.assertNotEqual(completed.returncode, 0, out)
            self.assertIn("HBC_STAGING=1 requires .hbc-staging", out)
            self.assertIn("refuse to start", out)
            self.assertLess(elapsed, 4)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_start_all_under_staging_never_starts_watcher(self):
        form = (ROOT / "shell" / "Form1.cs").read_text(encoding="utf-8")
        start = form.split("private async Task StartAll()", 1)[1].split("\n    private ", 1)[0]
        self.assertIn("HubIsStaging()", start)
        self.assertIn("Staging hub: Build watcher is off.", start)
        self.assertLess(start.find("HubIsStaging()"), start.find("BuildSessionOpen()"))
        self.assertIn("start all staging", start)
        self.assertNotIn("--continue", start)
        self.assertNotIn(server.WATCHER_PROMPT, start)
        hub = form.split("private async Task<bool> HubIsStaging()", 1)[1].split("\n    private ", 1)[0]
        self.assertIn("\"staging\"", hub)
        self.assertIn("SendHubState", hub)
        build = (ROOT / "build.html").read_text(encoding="utf-8")
        self.assertIn("applyStagingGate", build)
        self.assertIn("data.staging", build)
        self.assertIn("if (stagingHub)", build)
        self.assertIn("if (!off) addPane()", build)
        self.assertNotIn("addPane();", build.split("applyStagingGate", 1)[0].split("function addPane", 1)[-1])
        tab = (ROOT / "shell" / "ControlTab.cs").read_text(encoding="utf-8")
        focus = tab.split("internal void FocusBuildTab()", 1)[1].split("\n    private ", 1)[0]
        self.assertIn("if (lastStaging) return;", focus)
        self.assertLess(focus.find("lastStaging"), focus.find("buildView.Source"))
        restart = tab.split("private async Task RunRestartBuild()", 1)[1].split("\n    private ", 1)[0]
        self.assertIn("lastStaging", restart)
        self.assertIn("Staging hub: Build watcher is off.", restart)
        status = (ROOT / "hub" / "status.py").read_text(encoding="utf-8")
        self.assertIn("hbc_is_staging", status)
        self.assertIn('"staging"', status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
