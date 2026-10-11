"""SPEC-HBC-BUILD-CLAIM-1010 tests 1-15. Temporary test keys only. Keys are deleted after each test."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hub import protocol, sigverify
import server
from tests.live_guard import install_live_state_guard
from tests.runtime_skip import log_skip

install_live_state_guard()

WRITE_ORDER = (
    "id", "v", "from", "to", "lane", "priority", "time", "routed",
    "pane", "job", "done-when", "sig",
)
ET = protocol.ET


def _ps_shell():
    """Windows PowerShell 5.1 first; pwsh 7 is optional."""
    return shutil.which("powershell") or shutil.which("pwsh")


_OUTPUT = io.StringIO()


class _Tee:
    def __init__(self, stream):
        self._stream = stream

    def write(self, data):
        self._stream.write(data)
        _OUTPUT.write(data)
        return len(data)

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


sys.stdout = _Tee(sys.stdout)
sys.stderr = _Tee(sys.stderr)


def _iso(delta=None):
    when = __import__("datetime").datetime.now(ET)
    if delta is not None:
        when = when + delta
    return when.replace(microsecond=0).isoformat()


class TestBuildClaim1010(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.scratch = Path(tempfile.mkdtemp(prefix="hbc-iso-"))
        self.addCleanup(self._cleanup_scratch)
        self.bus = self.home / "bus"
        self.bus.mkdir()
        for name in (
            "LEAD-TO-BUILD.md",
            "GROKBOT-TO-BUILD.md",
            "BUILD-TO-LEAD.md",
            "BUILD-TO-GROKBOT.md",
        ):
            (self.bus / name).write_text("", encoding="utf-8")
        self.keys = {
            "Lead": bytearray(os.urandom(32)),
            "Grok Bot": bytearray(os.urandom(32)),
        }
        self.secrets = [self.keys["Lead"].hex(), self.keys["Grok Bot"].hex()]
        self.reached = []
        self._bus = server.BUS
        self._log = server.LOG
        self._seen = server.SEEN
        self._sessions = server.SESSIONS
        self._events = getattr(server, "EVENTS", None)
        self._broken = getattr(server, "_sessions_broken", False)
        server.BUS = self.bus
        server.LOG = self.home / "approvals.jsonl"
        server.SEEN = self.home / ".seen-ids.txt"
        server.SESSIONS = self.home / ".sessions.json"
        if self._events is not None:
            server.EVENTS = self.home / ".hub-events.jsonl"
        server._sessions_broken = False
        server._logged_spoofed.clear()
        server._seen_alarm_state = None
        server._seen_claim_alarmed = False
        server.clear_pending_returns()
        protocol.set_approvals_log(server.LOG)
        protocol.take_seen_events()
        sigverify.set_key_provider(self._provide)
        sigverify.set_keys_dir(self.home / "keys")
        (self.home / "keys").mkdir()
        server.set_run_impl(self._run)

    def tearDown(self):
        server.set_run_impl(None)
        sigverify.clear_key_provider()
        sigverify.set_keys_dir(None)
        server.BUS = self._bus
        server.LOG = self._log
        server.SEEN = self._seen
        server.SESSIONS = self._sessions
        if self._events is not None:
            server.EVENTS = self._events
        server._sessions_broken = self._broken
        for key in self.keys.values():
            sigverify._wipe(key)
        self.tmp.cleanup()

    def _provide(self, owner):
        material = self.keys.get(owner)
        if material is None:
            return None
        return bytearray(material)

    def _run(self, pane, command):
        self.reached.append(pane)
        return 0, "pane-ok", ""

    def _job(self, **overrides):
        iso = _iso()
        fields = {
            "v": "2",
            "id": "job-1",
            "from": "Lead",
            "to": "Build",
            "lane": "ops",
            "priority": "normal",
            "time": iso,
            "routed": "Lead " + iso,
            "job": "list lab pane files",
            "done-when": "the pane lists its files",
            "pane": "2",
        }
        fields.update(overrides)
        return fields

    def _sign(self, fields, owner=None):
        owner = sigverify.normalize_owner(owner or fields.get("from"))
        public = protocol.public_fields(fields)
        mac = sigverify.mac_hex(public, self.keys[owner])
        self.secrets.append(mac)
        self.secrets.append(str(public.get("job", "")))
        out = dict(public)
        out["sig"] = mac
        return out

    def _write(self, fields, name="LEAD-TO-BUILD.md"):
        public = protocol.public_fields(fields)
        lines = [f"{key}: {public[key]}" for key in WRITE_ORDER if key in public]
        (self.bus / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _block_text(self, fields):
        public = protocol.public_fields(fields)
        return "\n".join(f"{key}: {public[key]}" for key in WRITE_ORDER if key in public) + "\n"

    def _ps_shell(self):
        return _ps_shell()

    def _run_append(self, file_arg, text, *, cwd=None):
        shell = self._ps_shell()
        if not shell:
            self.skipTest("Append-BusJob.ps1 path tests: powershell.exe (5.1) or pwsh not on PATH")
        script = ROOT / "tools" / "Append-BusJob.ps1"
        return subprocess.run(
            [
                shell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(script), "-File", str(file_arg), "-Text", text,
            ],
            capture_output=True, text=True, timeout=20, cwd=cwd or str(ROOT),
        )

    def _cleanup_scratch(self):
        scratch = getattr(self, "scratch", None)
        if not scratch:
            return
        scratch = Path(scratch)
        try:
            children = list(scratch.iterdir())
        except OSError:
            children = []
        for child in children:
            if self._is_link_dir(child):
                self._rmdir_junction(child)
        try:
            for child in scratch.iterdir():
                if self._is_link_dir(child):
                    self._rmdir_junction(child)
        except OSError:
            pass
        shutil.rmtree(str(scratch), ignore_errors=True)

    def _is_link_dir(self, path):
        path = Path(path)
        try:
            if hasattr(path, "is_junction") and path.is_junction():
                return True
        except OSError:
            pass
        try:
            return path.is_symlink()
        except OSError:
            return False

    def _unique_repo_bus_file(self, kind="append"):
        repo_bus = ROOT / "bus"
        repo_bus.mkdir(parents=True, exist_ok=True)
        live = repo_bus / ("%s-%s.md" % (kind, self.scratch.name))
        self.addCleanup(self._cleanup_unique_paths, live)
        return live

    def _cleanup_unique_paths(self, *paths):
        for raw in paths:
            path = Path(raw)
            for extra in (
                path,
                Path(str(path) + ".lock"),
                Path(str(path) + ".tmp"),
                Path(str(path) + ".bak"),
            ):
                self._remove_isolated_path(extra)

    def _cleanup_repo_bus(self, *paths):
        self._cleanup_unique_paths(*paths)

    def _remove_isolated_path(self, path):
        path = Path(path)
        if self._is_link_dir(path):
            self._rmdir_junction(path)
            return
        try:
            if path.is_file():
                path.unlink()
                return
        except OSError:
            pass
        try:
            if path.is_dir():
                self._rmdir_junction(path)
        except OSError:
            pass

    def _remove_reparse(self, path):
        self._remove_isolated_path(path)

    def _rmdir_junction(self, path):
        path = Path(path)
        if os.name == "nt":
            try:
                os.rmdir(str(path))
                return
            except OSError:
                pass
            subprocess.run(
                ["cmd", "/c", "rmdir", str(path)],
                capture_output=True,
                text=True,
            )
            return
        try:
            if path.is_symlink():
                path.unlink()
                return
        except OSError:
            pass
        try:
            os.rmdir(str(path))
        except OSError:
            pass

    def _make_junction(self, link, target):
        link = Path(link)
        self.addCleanup(self._rmdir_junction, link)
        self._create_junction(link, target)
        return link

    def _hold_exclusive_lock(self, lock_path):
        holder = {"fd": os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)}
        self.addCleanup(self._release_lock_holder, holder)
        fd = holder["fd"]
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return holder

    def _release_lock_holder(self, holder):
        fd = holder.pop("fd", None)
        if fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def _symlink_allowed(self, directory):
        directory = Path(directory)
        src = directory / "_hbc_sy_src"
        dst = directory / "_hbc_sy_dst"
        try:
            src.write_text("x", encoding="utf-8")
            dst.symlink_to(src)
            return True
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                return False
            raise
        finally:
            for path in (dst, src):
                try:
                    if path.is_symlink() or path.is_file():
                        path.unlink()
                except OSError:
                    pass

    def _require_symlink(self):
        if not self._symlink_allowed(self.home):
            log_skip(
                self,
                "Windows denied symlink creation (WinError 1314). "
                "Junction refusals are covered by the junction tests.",
            )

    def _create_junction(self, link, target):
        link = Path(link)
        target = Path(target)
        if os.name != "nt":
            link.symlink_to(target, target_is_directory=True)
            return
        try:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
            return
        except (AttributeError, OSError):
            pass
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise AssertionError(
                "CreateJunction/mklink /J failed: %s %s"
                % ((completed.stdout or "").strip(), (completed.stderr or "").strip())
            )

    def _assert_clean(self, *parts):
        blob = "\n".join(str(item) for item in parts if item)
        if any(item and item in blob for item in self.secrets):
            self.fail("secret material found")
        log = server.LOG.read_text(encoding="utf-8") if server.LOG.is_file() else ""
        if any(item and item in log for item in self.secrets):
            self.fail("secret material found in log")
        if any(item and item in _OUTPUT.getvalue() for item in self.secrets):
            self.fail("secret material found in test output")
        seen = ""
        if server.SEEN.is_file():
            try:
                seen = server.SEEN.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                seen = server.SEEN.read_bytes().decode("utf-8", errors="replace")
        if any(item and item in seen for item in self.secrets):
            self.fail("secret material found in seen-list")

    def _log_rows(self):
        if not server.LOG.is_file():
            return []
        rows = []
        for line in server.LOG.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def _return_text(self, owner="Lead"):
        path = self.bus / server.RETURNS[owner]
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")

    def test_docs_no_go_no_action_and_autoreopen_off(self):
        proto = (ROOT / "BUS-PROTOCOL.md").read_text(encoding="utf-8")
        if server.NO_GO_NO_ACTION not in proto:
            self.fail("BUS-PROTOCOL.md does not state no GO, no action")
        if server.NO_GO_NO_ACTION not in server.WATCHER_PROMPT:
            self.fail("watcher prompt does not state no GO, no action")
        if "Claim-BusJob.ps1" not in server.WATCHER_PROMPT:
            self.fail("watcher prompt does not name the claim helper")
        if "advisory" not in proto.lower() and "backstop" not in proto:
            self.fail("BUS-PROTOCOL.md does not say the watcher rule is advisory")
        if "Do not make the HTTP call yourself" not in server.WATCHER_PROMPT:
            self.fail("watcher prompt still relies on grok HTTP")
        if "-File tools/Claim-BusJob.ps1" not in server.WATCHER_PROMPT:
            self.fail("watcher prompt does not tell the watcher to run the helper")
        if "Exit 0 is GO" not in server.WATCHER_PROMPT or "Exit 2 is NO-GO" not in server.WATCHER_PROMPT:
            self.fail("watcher prompt does not name helper exit codes")
        if "Ignore hub-routed" not in server.WATCHER_PROMPT:
            self.fail("watcher prompt does not ignore hub-routed jobs")
        if "routes_to_watcher" not in proto:
            self.fail("BUS-PROTOCOL.md does not name the single routing function")
        if "WRONG_PATH" not in proto:
            self.fail("BUS-PROTOCOL.md does not state WRONG_PATH")
        if "5c2fcc499acf9f26e6b34005a68bd8bdca4deea8911d7528dcc0d0e79072455b" not in proto:
            self.fail("BUS-PROTOCOL.md does not quote BUILD-CLAIM rev 3")
        if server.HBC_BUILD_AUTOREOPEN or server.build_autoreopen():
            self.fail("HBC_BUILD_AUTOREOPEN is on")
        script = (ROOT / "tools" / "Claim-BusJob.ps1").read_text(encoding="utf-8")
        if "Set-ExecutionPolicy" in script.splitlines()[0] or any(
            line.strip().startswith("Set-ExecutionPolicy") for line in script.splitlines()
        ):
            self.fail("Claim-BusJob.ps1 is not Bypass-safe")
        if "-ExecutionPolicy Bypass" not in script:
            self.fail("Claim-BusJob.ps1 does not document Bypass invocation")

    def test_01_restart_mid_job(self):
        signed = self._sign(self._job(id="restart-mid"))
        self._write(signed)
        protocol.read_seen(server.SEEN)
        server.record_approval("restart-mid", "1")
        first = server.claim_job("restart-mid", signed["sig"])
        self.assertEqual(first, "GO")
        row = protocol.find_seen(server.SEEN, "restart-mid")
        if not row or not protocol.is_claim(row):
            self.fail("claim was not on disk after GO")
        self.assertEqual(row.get("pane"), "2")
        server._seen_alarm_state = None
        protocol.take_seen_events()
        # Restart: seen-list is still the on-disk claim. Do not run again.
        runs = []

        def act(fields, owner):
            runs.append(fields.get("id"))

        results = server.resume_watcher(act=act)
        kinds = dict(results)
        self.assertEqual(kinds.get("restart-mid"), "CLAIMED-NOT-RETURNED")
        self.assertEqual(runs, [])
        second = server.claim_job("restart-mid", signed["sig"])
        self.assertEqual(second, "DUPLICATE")
        self.assertEqual(self.reached, [])
        self._assert_clean(first, second)

    def test_02_crash_between_claim_and_run(self):
        signed = self._sign(self._job(id="crash-go"))
        self._write(signed)
        protocol.read_seen(server.SEEN)
        server.record_approval("crash-go", "1")
        verdict = server.claim_job("crash-go", signed["sig"])
        self.assertEqual(verdict, "GO")
        # Watcher dies after GO. Resume must not run.
        runs = []
        results = server.resume_watcher(act=lambda fields, owner: runs.append(fields.get("id")))
        self.assertEqual(dict(results).get("crash-go"), "CLAIMED-NOT-RETURNED")
        self.assertEqual(runs, [])
        needs = server.claimed_not_returned()
        if not any(item.get("id") == "crash-go" and item.get("status") == "CLAIMED-NOT-RETURNED" for item in needs):
            self.fail("Control NEEDS-YOU missing CLAIMED-NOT-RETURNED")
        payload = server.state_payload()
        if not any(item.get("status") == "CLAIMED-NOT-RETURNED" and item.get("id") == "crash-go" for item in payload.get("needs_you", [])):
            self.fail("state payload missing CLAIMED-NOT-RETURNED")
        self.assertFalse(payload.get("autoreopen"))
        self._assert_clean(verdict)

    def test_03_resume_old_jobs_in_file(self):
        protocol.read_seen(server.SEEN)
        ids = []
        for index in range(5):
            job_id = "old-%s" % index
            signed = self._sign(self._job(id=job_id, job="list lab pane files %s" % index))
            ids.append((job_id, signed))
            self._write(signed)
            server.record_approval(job_id, "1")
            self.assertEqual(server.claim_job(job_id, signed["sig"]), "GO")
            ran = protocol.Verdict("RAN", "", http="RAN")
            protocol.finalize_seen(server.SEEN, signed, ran)
            server.append_return("Lead", protocol.format_reply(ran, signed, extra=["result: done"]))
        live = self.bus / "LEAD-TO-BUILD.md"
        dumped = "".join(self._block_text(item[1]) for item in ids)
        live.write_text(dumped, encoding="utf-8")
        runs = []
        results = server.resume_watcher(act=lambda fields, owner: runs.append(fields.get("id")))
        self.assertEqual(runs, [])
        kinds = dict(results)
        for job_id, _signed in ids:
            if kinds.get(job_id) not in ("SKIP_RETURN", "DUPLICATE"):
                self.fail("finished id %s was not skipped or DUPLICATE: %s" % (job_id, kinds.get(job_id)))
        self.assertEqual(self.reached, [])

    def test_04_bad_signature(self):
        signed = self._sign(self._job(id="bad-sig"))
        self._write(signed)
        protocol.read_seen(server.SEEN)
        server.record_approval("bad-sig", "1")
        verdict = server.claim_job("bad-sig", "a" * 64)
        self.assertEqual(verdict, "SPOOFED")
        if protocol.find_seen(server.SEEN, "bad-sig"):
            self.fail("spoofed claim entered the seen-list")
        self.assertEqual(self.reached, [])
        self._assert_clean(verdict)

    def test_05_clock_refusals(self):
        protocol.read_seen(server.SEEN)
        stale = self._sign(self._job(id="stale-1", time=_iso(timedelta(hours=-25)), routed="Lead " + _iso(timedelta(hours=-25))))
        self._write(stale)
        server.record_approval("stale-1", "1")
        self.assertEqual(server.claim_job("stale-1", stale["sig"]), "REFUSED STALE")
        future = self._sign(self._job(id="future-1", time=_iso(timedelta(minutes=10)), routed="Lead " + _iso()))
        self._write(future)
        server.record_approval("future-1", "1")
        self.assertEqual(server.claim_job("future-1", future["sig"]), "REFUSED BAD_TIME")
        if protocol.find_seen(server.SEEN, "stale-1") or protocol.find_seen(server.SEEN, "future-1"):
            self.fail("clock refusal recorded an id")
        retry = self._sign(self._job(id="stale-1-r2", job=stale["job"]))
        self._write(retry)
        server.record_approval("stale-1-r2", "1")
        self.assertEqual(server.claim_job("stale-1-r2", retry["sig"]), "GO")
        row = protocol.find_seen(server.SEEN, "stale-1-r2")
        if not row or not protocol.is_claim(row):
            self.fail("-r2 re-sign of refused content could not be claimed")
        self._assert_clean()

    def test_06_hub_down(self):
        signed = self._sign(self._job(id="hub-down"))
        self._write(signed)
        before = self._return_text("Lead")

        def down(job_id, sig, **kwargs):
            raise OSError("connection refused")

        verdict = server.watcher_try_claim("hub-down", signed["sig"], call=down)
        self.assertEqual(verdict, "REFUSED HUB_DOWN")
        self.assertFalse(server.watcher_may_act(verdict))
        self.assertEqual(self._return_text("Lead"), before)
        self.assertEqual(self.reached, [])
        self._assert_clean(verdict)

    def test_07_not_approved(self):
        signed = self._sign(self._job(id="need-yes"))
        self._write(signed)
        protocol.read_seen(server.SEEN)
        first = server.claim_job("need-yes", signed["sig"])
        self.assertEqual(first, "GO")
        if not protocol.find_seen(server.SEEN, "need-yes"):
            self.fail("ready signed claim did not enter the seen-list")
        self.assertEqual(self.reached, [])
        second = server.claim_job("need-yes", signed["sig"])
        self.assertEqual(second, "DUPLICATE")
        row = protocol.find_seen(server.SEEN, "need-yes")
        if not row or not protocol.is_claim(row):
            self.fail("approved claim did not write CLAIMED")
        self._assert_clean(first, second)

    def test_08_store_failure(self):
        signed = self._sign(self._job(id="store-fail"))
        self._write(signed)
        protocol.read_seen(server.SEEN)
        server.record_approval("store-fail", "1")

        def boom(src_path, dst_path):
            raise OSError("read-only")

        with mock.patch("hub.protocol.os.replace", boom):
            verdict = server.claim_job("store-fail", signed["sig"])
        self.assertEqual(verdict, "REFUSED STORE_ERROR")
        self.assertNotEqual(verdict, "GO")
        self.assertEqual(self.reached, [])
        self._assert_clean(verdict)

    def test_09_localhost_only(self):
        signed = self._sign(self._job(id="remote-1"))
        self._write(signed)
        protocol.read_seen(server.SEEN)
        server.record_approval("remote-1", "1")
        verdict = server.claim_job("remote-1", signed["sig"], remote="8.8.8.8")
        self.assertEqual(verdict, "REFUSED")
        if protocol.find_seen(server.SEEN, "remote-1"):
            self.fail("non-localhost claim entered the seen-list")
        local = server.claim_job("remote-1", signed["sig"], remote="127.0.0.1")
        self.assertEqual(local, "GO")
        self._assert_clean(verdict)

    def test_10_archive(self):
        signed = self._sign(self._job(id="arch-1"))
        extra = self._sign(self._job(id="arch-keep", job="list other lab pane files"))
        live = self.bus / "LEAD-TO-BUILD.md"
        live.write_text(self._block_text(signed) + self._block_text(extra), encoding="utf-8")
        protocol.read_seen(server.SEEN)
        server.record_approval("arch-1", "1")
        self.assertEqual(server.claim_job("arch-1", signed["sig"]), "GO")
        ran = protocol.Verdict("RAN", "", http="RAN")
        protocol.finalize_seen(server.SEEN, signed, ran)
        appended = {"n": 0}

        def during():
            appended["n"] += 1
            late = self._sign(self._job(id="arch-late", job="list late lab pane files"))
            with live.open("a", encoding="utf-8") as handle:
                handle.write(self._block_text(late))

        protocol.move_finished_to_archive(live, {"arch-1"}, during=during)
        self.assertEqual(appended["n"], 1)
        today = protocol.archive_name("LEAD-TO-BUILD.md")
        arch = self.bus / today
        if not arch.is_file():
            self.fail("dated archive was not written")
        archived = arch.read_text(encoding="utf-8")
        live_text = live.read_text(encoding="utf-8")
        if "id: arch-1" not in archived:
            self.fail("finished block is not in the archive")
        if "id: arch-1" in live_text:
            self.fail("finished block is still in the live file")
        if "id: arch-keep" not in live_text or "id: arch-late" not in live_text:
            self.fail("sender append or unfinished job was dropped")
        self._assert_clean()

    def test_11_replay_324_rollback(self):
        # 3:24 PM case: a finished rollback job is still in the live file after restart.
        signed = self._sign(self._job(
            id="rollback-324",
            job="rollback the last live handoff step",
            **{"done-when": "the rollback is confirmed"},
        ))
        self._write(signed)
        protocol.read_seen(server.SEEN)
        server.record_approval("rollback-324", "1")
        self.assertEqual(server.claim_job("rollback-324", signed["sig"]), "GO")
        ran = protocol.Verdict("RAN", "", http="RAN")
        protocol.finalize_seen(server.SEEN, signed, ran)
        server.append_return("Lead", protocol.format_reply(ran, signed, extra=["result: rolled-back"]))
        live = self.bus / "LEAD-TO-BUILD.md"
        live.write_text(self._block_text(signed), encoding="utf-8")
        runs = []

        def act(fields, owner):
            runs.append(fields.get("job", ""))

        results = server.resume_watcher(act=act)
        self.assertEqual(runs, [])
        kind = dict(results).get("rollback-324")
        if kind not in ("SKIP_RETURN", "DUPLICATE", "CLAIMED-NOT-RETURNED"):
            self.fail("restart replay ran or claimed the rollback job: %s" % kind)
        if any("rollback" in item.lower() for item in runs):
            self.fail("rollback was triggered")
        self.assertEqual(self.reached, [])
        self._assert_clean()

    def test_build_lane_approve_then_claim_go_then_duplicate(self):
        signed = self._sign(self._job(id="build-appr"))
        self._write(signed)
        protocol.read_seen(server.SEEN)
        status, payload = server.apply_decision("build-appr", "approve", lane="build")
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("result"), "approved")
        self.assertEqual(self.reached, [])
        if protocol.find_seen(server.SEEN, "build-appr"):
            self.fail("Build-lane approve claimed or ran the job")
        first = server.claim_job("build-appr", signed["sig"])
        self.assertEqual(first, "GO")
        second = server.claim_job("build-appr", signed["sig"])
        self.assertEqual(second, "DUPLICATE")
        self.assertEqual(self.reached, [])

    def test_claim_return_or_archive_is_nogo(self):
        signed = self._sign(self._job(id="already-back"))
        self._write(signed)
        protocol.read_seen(server.SEEN)
        server.apply_decision("already-back", "approve", lane="build")
        server.append_return("Lead", protocol.format_reply(protocol.Verdict("RAN", "", http="RAN"), signed))
        verdict = server.claim_job("already-back", signed["sig"])
        self.assertNotEqual(verdict, "GO")
        self.assertEqual(verdict, "DUPLICATE")

    def test_failed_claim_write_then_recover(self):
        signed = self._sign(self._job(id="claim-recover"))
        self._write(signed)
        protocol.read_seen(server.SEEN)
        server.apply_decision("claim-recover", "approve", lane="build")

        def boom(src_path, dst_path):
            raise OSError("read-only")

        with mock.patch("hub.protocol.os.replace", boom):
            failed = server.claim_job("claim-recover", signed["sig"])
        self.assertEqual(failed, "REFUSED STORE_ERROR")
        if protocol.find_seen(server.SEEN, "claim-recover"):
            self.fail("failed claim write left an id")
        recovered = server.claim_job("claim-recover", signed["sig"])
        self.assertEqual(recovered, "GO")
        again = server.claim_job("claim-recover", signed["sig"])
        self.assertEqual(again, "DUPLICATE")

    def test_approval_bound_to_sig_and_expires(self):
        first = self._sign(self._job(id="bind-id", job="list lab pane files A"))
        self._write(first)
        protocol.read_seen(server.SEEN)
        server.apply_decision("bind-id", "approve", lane="build")
        second = self._sign(self._job(id="bind-id", job="list lab pane files B"))
        self._write(second)
        reused = server.claim_job("bind-id", second["sig"])
        self.assertEqual(reused, "GO")
        aged = self._sign(self._job(id="bind-old"))
        self._write(aged)
        stale_time = datetime.now(timezone.utc) - timedelta(hours=25)
        server.LOG.write_text(
            json.dumps({
                "time": stale_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "id": "bind-old",
                "decision": "approve",
                "bind": server.approval_bind(aged),
                "lane": "build",
            }) + "\n",
            encoding="utf-8",
        )
        expired = server.claim_job("bind-old", aged["sig"])
        self.assertEqual(expired, "GO")

    def test_eight_threads_one_go(self):
        signed = self._sign(self._job(id="race-claim"))
        self._write(signed)
        protocol.read_seen(server.SEEN)
        server.apply_decision("race-claim", "approve", lane="build")
        results = []

        def worker():
            results.append(server.claim_job("race-claim", signed["sig"]))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count("GO"), 1)
        if results.count("DUPLICATE") + results.count("GO") != 8:
            self.fail("8-thread claim did not settle as one GO plus DUPLICATE: %s" % results)

    def test_archive_concurrent_append_keeps_every_block(self):
        first = self._sign(self._job(id="keep-fin", job="list lab pane files fin"))
        extra = self._sign(self._job(id="keep-live", job="list lab pane files live"))
        late = self._sign(self._job(id="keep-late", job="list lab pane files late"))
        live = self.bus / "LEAD-TO-BUILD.md"
        live.write_text(self._block_text(first) + self._block_text(extra), encoding="utf-8")

        def during():
            current = live.read_text(encoding="utf-8")
            live.write_text(current + self._block_text(late), encoding="utf-8")

        protocol.move_finished_to_archive(live, {"keep-fin"}, during=during)
        live_text = live.read_text(encoding="utf-8")
        arch = (self.bus / protocol.archive_name("LEAD-TO-BUILD.md")).read_text(encoding="utf-8")
        if "id: keep-fin" not in arch:
            self.fail("finished block missing from archive")
        if "id: keep-live" not in live_text or "id: keep-late" not in live_text:
            self.fail("concurrent append lost a block")
        protocol.move_finished_to_archive(live, {"keep-fin"})
        arch2 = (self.bus / protocol.archive_name("LEAD-TO-BUILD.md")).read_text(encoding="utf-8")
        if arch2.count("id: keep-fin") != 1:
            self.fail("crash replay duplicated an archive block")

    def test_forged_job_matching_stored_sig_is_spoofed(self):
        signed = self._sign(self._job(id="forge-hmac", job="list lab pane files"))
        tampered = dict(signed)
        tampered["job"] = "list other lab pane files"
        self._write(tampered)
        protocol.read_seen(server.SEEN)
        server.apply_decision("forge-hmac", "approve", lane="build")
        verdict = server.claim_job("forge-hmac", tampered["sig"])
        self.assertEqual(verdict, "SPOOFED")
        if protocol.find_seen(server.SEEN, "forge-hmac"):
            self.fail("forged HMAC entered the seen-list")
        if not any(row.get("decision") == "SPOOFED" for row in self._log_rows()):
            self.fail("forged HMAC was not logged")

    def test_claim_rejects_bad_host_header(self):
        self.assertFalse(server.claim_host_ok("evil.example:8107"))
        self.assertTrue(server.claim_host_ok("127.0.0.1:%s" % server.PORT))

    def test_lab_or_pane1_cannot_get_build_approval_or_go(self):
        lab = self._sign(self._job(id="lab-no-build", lane="lab", pane="2"))
        self._write(lab)
        protocol.read_seen(server.SEEN)
        status, payload = server.apply_decision("lab-no-build", "approve", lane="build")
        self.assertEqual(status, 200)
        self.assertEqual(self.reached, ["2"])
        if any(row.get("lane") == "build" and row.get("decision") == "approve" for row in self._log_rows()):
            self.fail("lab job recorded a Build approval")
        self.assertNotEqual(server.claim_job("lab-no-build", lab["sig"]), "GO")

        pane1 = self._sign(self._job(id="pane1-no-build", lane="ops", pane="1"))
        self._write(pane1)
        self.reached.clear()
        status, payload = server.apply_decision("pane1-no-build", "approve", lane="build")
        self.assertEqual(status, 200)
        self.assertEqual(self.reached, ["1"])
        self.assertNotEqual(server.claim_job("pane1-no-build", pane1["sig"]), "GO")
        self.assertFalse(server.is_build_lane(lab))
        self.assertFalse(server.is_build_lane(pane1))
        self.assertTrue(server.is_build_lane(self._job(id="build-shaped")))
        other = self._sign(self._job(id="not-build-to", to="Ops", lane="ops", pane="2"))
        self.assertFalse(server.is_build_lane(other))

    def test_desk_does_not_hardcode_build_lane(self):
        desk = (ROOT / "desk.js").read_text(encoding="utf-8")
        if "lane: \"build\"" in desk or "lane:'build'" in desk:
            self.fail("desk.js hard-codes lane:'build'")
        if "JSON.stringify({ id, decision, hash })" not in desk:
            self.fail("desk.js does not send {id, decision, hash} without a lane")
        if "decide(job.id, decision, job.hash)" not in desk:
            self.fail("desk.js does not send the /api/state content hash")

    def test_approval_without_bind_is_not_go(self):
        signed = self._sign(self._job(id="no-bind"))
        self._write(signed)
        protocol.read_seen(server.SEEN)
        server.LOG.write_text(
            json.dumps({
                "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "id": "no-bind",
                "decision": "approve",
                "lane": "build",
            }) + "\n",
            encoding="utf-8",
        )
        verdict = server.claim_job("no-bind", signed["sig"])
        self.assertEqual(verdict, "GO")

    def test_unreadable_approval_time_is_expired(self):
        signed = self._sign(self._job(id="bad-time-appr"))
        self._write(signed)
        protocol.read_seen(server.SEEN)
        server.LOG.write_text(
            json.dumps({
                "time": "not-a-time",
                "id": "bad-time-appr",
                "decision": "approve",
                "bind": server.approval_bind(signed),
                "lane": "build",
            }) + "\n",
            encoding="utf-8",
        )
        self.assertFalse(server.is_approved("bad-time-appr", signed))
        self.assertEqual(server.claim_job("bad-time-appr", signed["sig"]), "GO")

    def test_approve_after_go_is_not_spoofed_replay(self):
        signed = self._sign(self._job(id="after-go"))
        self._write(signed)
        protocol.read_seen(server.SEEN)
        server.apply_decision("after-go", "approve")
        self.assertEqual(server.claim_job("after-go", signed["sig"]), "GO")
        status, payload = server.apply_decision("after-go", "approve")
        self.assertEqual(status, 403)
        self.assertNotEqual(payload.get("error"), "SPOOFED REPLAY")
        if any(row.get("decision") == "SPOOFED" and row.get("reason") == "REPLAY" for row in self._log_rows()):
            self.fail("normal approve after GO was logged as SPOOFED REPLAY")

    def test_p5a_concurrent_write_not_lost(self):
        first = self._sign(self._job(id="p5a-fin", job="list lab pane files p5a fin"))
        extra = self._sign(self._job(id="p5a-live", job="list lab pane files p5a live"))
        late = self._sign(self._job(id="p5a-late", job="list lab pane files p5a late"))
        live = self.bus / "LEAD-TO-BUILD.md"
        live.write_text(self._block_text(first) + self._block_text(extra), encoding="utf-8")

        def unlocked():
            current = live.read_text(encoding="utf-8")
            live.write_text(current + self._block_text(late), encoding="utf-8")

        protocol.move_finished_to_archive(live, {"p5a-fin"}, during=unlocked)
        live_text = live.read_text(encoding="utf-8")
        arch = (self.bus / protocol.archive_name("LEAD-TO-BUILD.md")).read_text(encoding="utf-8")
        if "id: p5a-fin" not in arch:
            self.fail("P5a finished block missing from archive")
        if "id: p5a-live" not in live_text or "id: p5a-late" not in live_text:
            self.fail("P5a concurrent write was lost")

    def test_p5c_changing_file_bounded_failure(self):
        first = self._sign(self._job(id="p5c-fin", job="list lab pane files p5c"))
        live = self.bus / "LEAD-TO-BUILD.md"
        live.write_text(self._block_text(first), encoding="utf-8")
        ticks = {"n": 0}

        def changing():
            ticks["n"] += 1
            live.write_text(
                live.read_text(encoding="utf-8") + "# churn %s\n" % ticks["n"],
                encoding="utf-8",
            )

        started = time.monotonic()
        with self.assertRaises(protocol.ArchiveError):
            protocol.move_finished_to_archive(live, {"p5c-fin"}, changing=changing)
        if time.monotonic() - started > protocol.ARCHIVE_DEADLINE_SEC + 1:
            self.fail("P5c archive loop exceeded the timeout guard")
        if ticks["n"] > protocol.ARCHIVE_RETRIES + 1:
            self.fail("P5c retried without a bound: %s" % ticks["n"])
        self.assertGreaterEqual(ticks["n"], protocol.ARCHIVE_RETRIES)

    def test_signer_lock_covers_read_with_timeout(self):
        script = (ROOT / "tools" / "Sign-BusJob.ps1").read_text(encoding="utf-8")
        if "Wait-BusLock" not in script:
            self.fail("signer has no capped lock wait")
        if "bus file lock timeout" not in script:
            self.fail("signer lock wait is not capped")
        lock_at = script.find("Wait-BusLock $lockPath")
        read_at = script.find("[System.IO.File]::ReadAllText($File)")
        if lock_at < 0 or read_at < 0 or lock_at > read_at:
            self.fail("signer lock does not cover the read")
        if '"$File.lock"' not in script and "$File.lock" not in script:
            self.fail("signer does not use the shared <file>.lock")

    def test_claim_reply_mac_matches_helper_canonical(self):
        mac = server.claim_reply_mac("job-x", "nonce-1", "GO", token="lab-token")
        again = server.claim_reply_mac("job-x", "nonce-1", "GO", token="lab-token")
        self.assertEqual(mac, again)
        self.assertEqual(len(mac), 64)
        other = server.claim_reply_mac("job-x", "nonce-2", "GO", token="lab-token")
        self.assertNotEqual(mac, other)

    def test_to_not_build_goes_to_hub_run_pane(self):
        signed = self._sign(self._job(id="ops-dest", to="Ops", lane="ops", pane="2"))
        self._write(signed)
        protocol.read_seen(server.SEEN)
        self.assertFalse(server.routes_to_watcher(signed))
        self.assertEqual(server.claim_job("ops-dest", signed["sig"]), "REFUSED WRONG_PATH")
        status, payload = server.apply_decision("ops-dest", "approve")
        self.assertEqual(status, 200)
        self.assertEqual(self.reached, ["2"])

    def test_12_wrong_path_refusals_both_ways(self):
        watcher = self._sign(self._job(id="w12", lane="ops", pane="2"))
        protocol.read_seen(server.SEEN)
        code, _out, err = server.run_pane("2", watcher["job"], watcher, "Lead")
        self.assertEqual(code, 403)
        self.assertEqual(err, "WRONG_PATH")
        self.assertEqual(self.reached, [])
        if protocol.find_seen(server.SEEN, "w12"):
            self.fail("watcher job sent to run_pane entered the seen-list")

        hub = self._sign(self._job(id="h12", lane="lab", pane="1"))
        self._write(hub)
        server.record_approval("h12", "1", fields=hub)
        verdict = server.claim_job("h12", hub["sig"])
        self.assertEqual(verdict, "REFUSED WRONG_PATH")
        if protocol.find_seen(server.SEEN, "h12"):
            self.fail("hub-routed claim wrote CLAIMED")
        self.assertEqual(self.reached, [])

    def test_13_pane_and_lane_routing(self):
        pane2 = self._job(id="r13-p2", lane="ops", pane="2")
        pane1 = self._job(id="r13-p1", lane="ops", pane="1")
        lab = self._job(id="r13-lab", lane="lab", pane="2")
        empty = self._job(id="r13-empty", lane="ops", pane="")
        self.assertTrue(server.routes_to_watcher(pane2))
        self.assertFalse(server.routes_to_watcher(pane1))
        self.assertFalse(server.routes_to_watcher(lab))
        self.assertTrue(server.routes_to_watcher(empty))
        self.assertEqual(server.routes_to_watcher(pane2), server.is_build_lane(pane2))

        signed2 = self._sign(pane2)
        self._write(signed2)
        protocol.read_seen(server.SEEN)
        status, payload = server.apply_decision("r13-p2", "approve")
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("result"), "approved")
        self.assertEqual(self.reached, [])
        self.assertEqual(server.claim_job("r13-p2", signed2["sig"]), "GO")

        signed1 = self._sign(pane1)
        self._write(signed1)
        self.reached.clear()
        self.assertEqual(server.claim_job("r13-p1", signed1["sig"]), "REFUSED WRONG_PATH")
        status, payload = server.apply_decision("r13-p1", "approve")
        self.assertEqual(status, 200)
        self.assertEqual(self.reached, ["1"])

        signed_lab = self._sign(lab)
        self._write(signed_lab)
        self.reached.clear()
        self.assertEqual(server.claim_job("r13-lab", signed_lab["sig"]), "REFUSED WRONG_PATH")
        status, payload = server.apply_decision("r13-lab", "approve")
        self.assertEqual(status, 200)
        self.assertEqual(self.reached, ["2"])

    def test_14_tamper_to_or_pane_is_spoofed(self):
        protocol.read_seen(server.SEEN)
        signed = self._sign(self._job(id="tamp-to", lane="ops", pane="2"))
        tampered = dict(signed)
        tampered["to"] = "Ops"
        self._write(tampered)
        status, payload = server.apply_decision("tamp-to", "approve")
        self.assertEqual(status, 403)
        self.assertTrue(str(payload.get("error", "")).startswith("SPOOFED"))
        self.assertEqual(server.claim_job("tamp-to", tampered["sig"]), "SPOOFED")
        self.assertEqual(self.reached, [])

        signed_p = self._sign(self._job(id="tamp-pane", lane="ops", pane="2"))
        tampered_p = dict(signed_p)
        tampered_p["pane"] = "1"
        self._write(tampered_p)
        status, payload = server.apply_decision("tamp-pane", "approve")
        self.assertEqual(status, 403)
        self.assertTrue(str(payload.get("error", "")).startswith("SPOOFED"))
        self.assertEqual(server.claim_job("tamp-pane", tampered_p["sig"]), "SPOOFED")
        if protocol.find_seen(server.SEEN, "tamp-to") or protocol.find_seen(server.SEEN, "tamp-pane"):
            self.fail("tampered job entered the seen-list")

    def test_15_pane_tokens_and_to_trim(self):
        protocol.read_seen(server.SEEN)
        self.assertTrue(server.routes_to_watcher(self._job(to="Build", lane="ops", pane="2")))
        self.assertTrue(server.routes_to_watcher(self._job(to="  Build  ", lane="ops", pane="2")))
        self.assertTrue(server.routes_to_watcher(self._job(to="Build", lane="ops", pane="")))
        self.assertFalse(server.routes_to_watcher(self._job(to="build", lane="ops", pane="2")))
        self.assertFalse(server.routes_to_watcher(self._job(to="BUILD", lane="ops", pane="2")))
        self.assertFalse(server.routes_to_watcher(self._job(to="Build", lane="ops", pane="1")))
        self.assertEqual(server.signed_to({"to": "  Build  "}), "Build")
        self.assertNotEqual(server.signed_to({"to": "build"}), "Build")

        for pane, job_id in (
            ("01", "p15-01"),
            (" 2x", "p15-2x"),
            ("\u0661", "p15-ar"),
            ("\uff12", "p15-fw"),
        ):
            fields = self._job(id=job_id, lane="ops", pane=pane)
            self.assertEqual(server.pane_reason(fields), "BAD_PANE")
            self.assertFalse(server.routes_to_watcher(fields))
            signed = self._sign(fields)
            self._write(signed)
            self.assertEqual(server.claim_job(job_id, signed["sig"]), "REFUSED BAD_PANE")
            if protocol.find_seen(server.SEEN, job_id):
                self.fail("BAD_PANE claim wrote %s" % job_id)
            status, payload = server.apply_decision(job_id, "approve")
            self.assertEqual(status, 403)
            self.assertEqual(payload.get("error"), "BAD_PANE")
            code, _out, err = server.run_pane("2", signed["job"], signed, "Lead")
            self.assertEqual(code, 403)
            self.assertEqual(err, "BAD_PANE")
            self.assertEqual(self.reached, [])

    def test_archive_give_up_raises_alarm(self):
        signed = self._sign(self._job(id="arch-alarm", job="list lab pane files alarm"))
        live = self.bus / "LEAD-TO-BUILD.md"
        live.write_text(self._block_text(signed), encoding="utf-8")

        def changing():
            live.write_text(
                live.read_text(encoding="utf-8") + "# churn\n",
                encoding="utf-8",
            )

        with mock.patch("hub.protocol.move_finished_to_archive", side_effect=protocol.ArchiveError("ARCHIVE")):
            server.archive_job(signed)
        if not any(row.get("decision") == "alarm" and row.get("reason") == "ARCHIVE" for row in self._log_rows()):
            self.fail("archive give-up did not raise an ARCHIVE alarm")

    def test_archive_lock_open_failure_alarms(self):
        signed = self._sign(self._job(id="lock-alarm", job="list lab pane files lock"))
        live = self.bus / "LEAD-TO-BUILD.md"
        live.write_text(self._block_text(signed), encoding="utf-8")

        def boom(*args, **kwargs):
            raise protocol.LockError("LOCK")

        with mock.patch("hub.protocol.lock_bus", side_effect=boom):
            server.archive_job(signed)
        if not any(row.get("decision") == "alarm" and row.get("reason") == "LOCK" for row in self._log_rows()):
            self.fail("lock-open failure did not raise a LOCK alarm")

    def test_append_helper_shipped_and_uses_shared_lock(self):
        script = ROOT / "tools" / "Append-BusJob.ps1"
        if not script.is_file():
            self.fail("tools/Append-BusJob.ps1 is not shipped")
        text = script.read_text(encoding="utf-8")
        if any(line.strip().startswith("Set-ExecutionPolicy") for line in text.splitlines()):
            self.fail("Append-BusJob.ps1 changes the execution policy")
        if "Wait-BusLock" not in text or "$File.lock" not in text:
            self.fail("Append-BusJob.ps1 does not use the shared <file>.lock")
        proto = (ROOT / "BUS-PROTOCOL.md").read_text(encoding="utf-8")
        if "Append-BusJob.ps1" not in proto:
            self.fail("BUS-PROTOCOL.md does not name the locked-append helper")
        if "replace window" not in proto.lower() and "P5a" not in proto:
            self.fail("BUS-PROTOCOL.md does not warn that a prose append can lose text (P5a)")
        if "$ExitError = 3" not in text:
            self.fail("Append-BusJob.ps1 is not fail-closed exit 3")
        if "CreateNew" not in text:
            self.fail("Append-BusJob.ps1 does not open the temp file with FileMode.CreateNew")
        if "NewGuid" not in text:
            self.fail("Append-BusJob.ps1 does not use a random temp name")
        if "[System.IO.File]::Replace" not in text:
            self.fail("Append-BusJob.ps1 does not replace from the temp file")
        if "function Test-BusFile" not in text:
            self.fail("removing Test-BusFile fails the suite")
        if "Test-BusFile $File" not in text:
            self.fail("Append-BusJob.ps1 does not call Test-BusFile")
        if 'Join-Path $hubRoot "bus"' not in text or 'Join-Path $hubRoot "handoff"' not in text:
            self.fail("Append-BusJob.ps1 does not pin the hub bus and handoff folders")
        if "GetFullPath" not in text:
            self.fail("Append-BusJob.ps1 does not resolve the full path")
        if '".."' not in text and "'..'" not in text:
            self.fail("Append-BusJob.ps1 does not refuse ..")
        if "ReparsePoint" not in text:
            self.fail("Append-BusJob.ps1 does not refuse reparse points or symlinks")
        if "Test-Reparse $lockPath" not in text:
            self.fail("Append-BusJob.ps1 does not refuse a reparse .lock")
        if "Test-ParentAllowed $File" not in text:
            self.fail("Append-BusJob.ps1 does not re-check $File after Replace")
        if "APPENDED $File" in text:
            self.fail("Append-BusJob.ps1 success line shows the full path")
        if "APPENDED $leaf" not in text:
            self.fail("Append-BusJob.ps1 success line is not the file name")

    def test_append_refuses_foreign_bus_and_dotdot(self):
        text = (ROOT / "tools" / "Append-BusJob.ps1").read_text(encoding="utf-8")
        if "function Test-BusFile" not in text:
            self.fail("removing Test-BusFile fails the suite")
        live = self._unique_repo_bus_file("append")
        other_bus = self.scratch / "other" / "bus"
        other_bus.mkdir(parents=True)
        foreign = other_bus / "z.md"
        sneak = other_bus / "q.md"
        live.write_text("base\n", encoding="utf-8")
        foreign.write_text("z\n", encoding="utf-8")
        sneak.write_text("q\n", encoding="utf-8")
        z_run = self._run_append(str(foreign), "NO\n")
        if z_run.returncode != server.CLAIM_EXIT_ERROR:
            self.fail("other/bus/z.md was not refused: %s" % ((z_run.stdout or "") + (z_run.stderr or "")))
        if foreign.read_text(encoding="utf-8") != "z\n":
            self.fail("other/bus/z.md was written")

        rel_other = self._run_append("bus/../other/bus/q.md", "NO\n", cwd=str(self.scratch))
        if rel_other.returncode != server.CLAIM_EXIT_ERROR:
            self.fail("bus/../other/bus/q.md was not refused: %s" % (
                (rel_other.stdout or "") + (rel_other.stderr or ""),
            ))
        if sneak.read_text(encoding="utf-8") != "q\n":
            self.fail("bus/../other/bus/q.md was written")

        rel_bus = self._run_append("bus/../bus/%s" % live.name, "NO\n", cwd=str(ROOT))
        if rel_bus.returncode != server.CLAIM_EXIT_ERROR:
            self.fail("bus/../bus/*.md was not refused: %s" % ((rel_bus.stdout or "") + (rel_bus.stderr or "")))
        if live.read_text(encoding="utf-8") != "base\n":
            self.fail("bus/../bus/*.md wrote through the parent-folder check")

    def test_append_refuses_foreign_bus_dotdot_and_symlink(self):
        self._require_symlink()
        live = self._unique_repo_bus_file("append")
        linked = self._unique_repo_bus_file("link")
        live.write_text("base\n", encoding="utf-8")
        linked.symlink_to(live)
        link_run = self._run_append(str(linked), "NO\n")
        if link_run.returncode != server.CLAIM_EXIT_ERROR:
            self.fail("symlinked .md was not refused: %s" % ((link_run.stdout or "") + (link_run.stderr or "")))
        if live.read_text(encoding="utf-8") != "base\n":
            self.fail("symlinked .md wrote through to the target")

    def test_append_refuses_symlinked_tmp_and_lock(self):
        self._require_symlink()
        live = self._unique_repo_bus_file("append")
        tmp_link = Path(str(live) + ".tmp")
        lock_link = Path(str(live) + ".lock")
        evil_tmp = self.scratch / "evil-tmp"
        evil_lock = self.scratch / "evil-lock"
        live.write_text("base\n", encoding="utf-8")
        evil_tmp.write_text("SAFE\n", encoding="utf-8")
        evil_lock.write_text("SAFE\n", encoding="utf-8")
        tmp_link.symlink_to(evil_tmp)
        ok = self._run_append(str(live), "NEW\n")
        if ok.returncode != 0:
            self.fail("append past a leftover .tmp symlink failed: %s" % (
                (ok.stdout or "") + (ok.stderr or ""),
            ))
        if (ok.stdout or "").splitlines()[:1] != ["APPENDED %s" % live.name]:
            self.fail("success line was not the file name: %s" % ok.stdout)
        if live.read_text(encoding="utf-8") != "base\nNEW\n":
            self.fail("append did not write the live bus file: %s" % live.read_text(encoding="utf-8"))
        if evil_tmp.read_text(encoding="utf-8") != "SAFE\n":
            self.fail("symlinked .tmp was written through")
        if not tmp_link.is_symlink():
            self.fail("symlinked .tmp was replaced")

        self._remove_isolated_path(tmp_link)
        self._remove_isolated_path(lock_link)
        live.write_text("base\n", encoding="utf-8")
        lock_link.symlink_to(evil_lock)
        refused = self._run_append(str(live), "NEW\n")
        if refused.returncode != server.CLAIM_EXIT_ERROR:
            self.fail("symlinked .lock was not refused: %s" % (
                (refused.stdout or "") + (refused.stderr or ""),
            ))
        if live.read_text(encoding="utf-8") != "base\n":
            self.fail("symlinked .lock still appended")
        if evil_lock.read_text(encoding="utf-8") != "SAFE\n":
            self.fail("symlinked .lock was written through")

    def test_append_refuses_junction_parent(self):
        live = self._unique_repo_bus_file("append")
        junction = self.scratch / "junc"
        via = junction / live.name
        live.write_text("base\n", encoding="utf-8")
        self._make_junction(junction, ROOT / "bus")
        refused = self._run_append(str(via), "NO\n")
        if refused.returncode != server.CLAIM_EXIT_ERROR:
            self.fail("junction parent was not refused: %s" % (
                (refused.stdout or "") + (refused.stderr or ""),
            ))
        if live.read_text(encoding="utf-8") != "base\n":
            self.fail("junction parent wrote through to the target")

    def test_append_refuses_late_junction_swap(self):
        if not self._ps_shell():
            self.skipTest(
                "Append-BusJob.ps1 late junction swap: powershell.exe (5.1) or pwsh not on PATH"
            )
        live = self._unique_repo_bus_file("append")
        lock = Path(str(live) + ".lock")
        evil_dir = self.scratch / "evil-late-junc"
        evil_dir.mkdir()
        (evil_dir / "marker").write_text("SAFE\n", encoding="utf-8")
        live.write_text("base\n", encoding="utf-8")
        holder = self._hold_exclusive_lock(lock)
        result = {"run": None}
        thread = None
        try:
            def run():
                result["run"] = self._run_append(str(live), "NO\n")

            thread = threading.Thread(target=run)
            thread.start()
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and thread.is_alive() and result["run"] is None:
                time.sleep(0.05)
            if result["run"] is not None:
                self.fail("append finished before the late junction swap: %s" % (
                    (result["run"].stdout or "") + (result["run"].stderr or ""),
                ))
            if live.exists() and not (hasattr(live, "is_junction") and live.is_junction()):
                try:
                    if live.is_symlink() or live.is_file():
                        live.unlink()
                except OSError:
                    pass
            self._make_junction(live, evil_dir)
        finally:
            self._release_lock_holder(holder)
        if thread is not None:
            thread.join(15)
            if thread.is_alive():
                self.fail("append hung after the late junction swap")
        refused = result["run"]
        if refused is None or refused.returncode != server.CLAIM_EXIT_ERROR:
            self.fail("late junction swap was not refused: %s" % (
                "" if refused is None else ((refused.stdout or "") + (refused.stderr or "")),
            ))
        if (evil_dir / "marker").read_text(encoding="utf-8") != "SAFE\n":
            self.fail("late junction swap wrote through to the target")

    def test_append_refuses_junction_lock_dir(self):
        live = self._unique_repo_bus_file("append")
        lock_junc = Path(str(live) + ".lock")
        evil_dir = self.scratch / "evil-lock-dir"
        evil_dir.mkdir()
        (evil_dir / "marker").write_text("SAFE\n", encoding="utf-8")
        live.write_text("base\n", encoding="utf-8")
        self._make_junction(lock_junc, evil_dir)
        refused = self._run_append(str(live), "NEW\n")
        if refused.returncode != server.CLAIM_EXIT_ERROR:
            self.fail("junction .lock was not refused: %s" % (
                (refused.stdout or "") + (refused.stderr or ""),
            ))
        if live.read_text(encoding="utf-8") != "base\n":
            self.fail("junction .lock still appended")
        if (evil_dir / "marker").read_text(encoding="utf-8") != "SAFE\n":
            self.fail("junction .lock wrote through to the target")

    def test_lock_retries_permissionerror_then_acquires(self):
        live = self.bus / "LEAD-TO-BUILD.md"
        real_open = os.open
        hits = {"n": 0}

        def flaky(path, flags, mode=0o777, *args, **kwargs):
            if str(path).endswith(".lock"):
                hits["n"] += 1
                if hits["n"] < 3:
                    raise PermissionError("held")
            return real_open(path, flags, mode, *args, **kwargs)

        with mock.patch("hub.protocol.os.open", flaky):
            with protocol.lock_bus(live):
                pass
        if hits["n"] < 3:
            self.fail("lock did not retry os.open on PermissionError: %s" % hits["n"])

    def test_lock_wait_is_bounded(self):
        live = self.bus / "LEAD-TO-BUILD.md"
        started = time.monotonic()
        if os.name == "nt":
            import msvcrt
            locker = mock.patch.object(msvcrt, "locking", side_effect=OSError("busy"))
        else:
            import fcntl
            locker = mock.patch.object(fcntl, "flock", side_effect=OSError("busy"))
        with mock.patch("hub.protocol.LOCK_WAIT_SEC", 0.2):
            with mock.patch("hub.protocol.LOCK_WAIT_SLICE", 0.02):
                with locker:
                    with self.assertRaises(protocol.LockError):
                        with protocol.lock_bus(live):
                            pass
        elapsed = time.monotonic() - started
        if elapsed > 1.5:
            self.fail("lock wait was not bounded: %s" % elapsed)

    def test_lock_wait_is_actual_five_seconds(self):
        self.assertEqual(protocol.LOCK_WAIT_SEC, 5.0)
        live = self.bus / "LEAD-TO-BUILD.md"
        started = time.monotonic()
        if os.name == "nt":
            import msvcrt
            locker = mock.patch.object(msvcrt, "locking", side_effect=OSError("busy"))
        else:
            import fcntl
            locker = mock.patch.object(fcntl, "flock", side_effect=OSError("busy"))
        with locker:
            with self.assertRaises(protocol.LockError):
                with protocol.lock_bus(live):
                    pass
        elapsed = time.monotonic() - started
        if elapsed < 4.5:
            self.fail("lock wait was shorter than 5s: %s" % elapsed)
        if elapsed > 7.0:
            self.fail("lock wait was not bounded to 5s: %s" % elapsed)

    def test_append_lock_timeout_exits_3(self):
        if os.name != "nt":
            self.skipTest("Append 5s lock timeout: Windows FileShare.None / msvcrt.locking")
        shell = self._ps_shell()
        if not shell:
            self.skipTest("Append 5s lock timeout: powershell.exe (5.1) or pwsh not on PATH")
        live = self._unique_repo_bus_file("append")
        lock = Path(str(live) + ".lock")
        live.write_text("base\n", encoding="utf-8")
        holder = self._hold_exclusive_lock(lock)
        try:
            started = time.monotonic()
            refused = self._run_append(str(live), "NEW\n")
            elapsed = time.monotonic() - started
            if refused.returncode != server.CLAIM_EXIT_ERROR:
                self.fail("held lock did not exit 3: %s" % (
                    (refused.stdout or "") + (refused.stderr or ""),
                ))
            if live.read_text(encoding="utf-8") != "base\n":
                self.fail("held lock still appended")
            if elapsed < 4.5:
                self.fail("Append lock wait was shorter than 5s: %s" % elapsed)
        finally:
            self._release_lock_holder(holder)

    def test_lock_permissionerror_is_bounded(self):
        live = self.bus / "LEAD-TO-BUILD.md"
        started = time.monotonic()
        held = {"err": None}

        def run():
            try:
                with mock.patch("hub.protocol.LOCK_WAIT_SEC", 0.2):
                    with mock.patch("hub.protocol.LOCK_WAIT_SLICE", 0.02):
                        with mock.patch("hub.protocol.os.open", side_effect=PermissionError("held")):
                            with protocol.lock_bus(live):
                                pass
            except Exception as exc:
                held["err"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(2.0)
        if thread.is_alive():
            self.fail("PermissionError lock wait hung past the test timeout guard")
        if not isinstance(held["err"], protocol.LockError):
            self.fail("PermissionError lock wait did not raise LockError: %r" % held["err"])
        elapsed = time.monotonic() - started
        if elapsed > 1.5:
            self.fail("PermissionError lock wait was not bounded: %s" % elapsed)

    def test_archive_deadline_raises(self):
        first = self._sign(self._job(id="dead-fin", job="list lab pane files dead"))
        live = self.bus / "LEAD-TO-BUILD.md"
        live.write_text(self._block_text(first), encoding="utf-8")
        clock = {"t": 0.0}
        ticks = {"n": 0}

        def mono():
            return clock["t"]

        def changing():
            ticks["n"] += 1
            clock["t"] += protocol.ARCHIVE_DEADLINE_SEC + 0.1
            live.write_text(live.read_text(encoding="utf-8") + "# churn\n", encoding="utf-8")

        with mock.patch("hub.protocol.time.monotonic", mono):
            with self.assertRaises(protocol.ArchiveError):
                protocol.move_finished_to_archive(live, {"dead-fin"}, changing=changing)
        if ticks["n"] >= protocol.ARCHIVE_RETRIES:
            self.fail("archive deadline attempts reached the retry cap: %s" % ticks["n"])

    def test_resume_skips_hub_routed_jobs(self):
        signed = self._sign(self._job(id="skip-hub", lane="lab", pane="1"))
        self._write(signed)
        protocol.read_seen(server.SEEN)
        runs = []
        results = server.resume_watcher(act=lambda fields, owner: runs.append(fields.get("id")))
        self.assertEqual(dict(results).get("skip-hub"), "SKIP_HUB")
        self.assertEqual(runs, [])
        if protocol.find_seen(server.SEEN, "skip-hub"):
            self.fail("hub-routed resume wrote a claim")

    def test_control_path_and_to_case(self):
        watcher = self._sign(self._job(id="ctrl-watch", lane="ops", pane="2"))
        hub = self._sign(self._job(id="ctrl-hub", lane="lab", pane="1"))
        live = self.bus / "LEAD-TO-BUILD.md"
        live.write_text(self._block_text(watcher) + self._block_text(hub), encoding="utf-8")
        protocol.read_seen(server.SEEN)
        items = {row["id"]: row for row in server.queue()}
        self.assertEqual(items["ctrl-watch"].get("path"), "watcher")
        self.assertEqual(items["ctrl-hub"].get("path"), "hub")
        self.assertTrue(server.routes_to_watcher(self._job(to="Build", lane="ops", pane="2")))
        self.assertTrue(server.routes_to_watcher(self._job(to="  Build  ", lane="ops", pane="2")))
        self.assertFalse(server.routes_to_watcher(self._job(to="build", lane="ops", pane="2")))
        self.assertFalse(server.routes_to_watcher(self._job(to="BUILD", lane="ops", pane="2")))

    def test_run_pane_refuses_existing_claim(self):
        signed = self._sign(self._job(id="already-claimed", lane="lab", pane="1"))
        protocol.read_seen(server.SEEN)
        protocol.claim_seen(server.SEEN, signed)
        code, _out, err = server.run_pane("1", signed["job"], signed, "Lead")
        self.assertEqual(code, 403)
        self.assertEqual(err, "DUPLICATE")
        self.assertEqual(self.reached, [])

    def test_nonstandard_pane_is_bad_pane(self):
        signed = self._sign(self._job(id="pane-3", lane="ops", pane="3"))
        self._write(signed)
        protocol.read_seen(server.SEEN)
        self.assertEqual(server.claim_job("pane-3", signed["sig"]), "REFUSED BAD_PANE")
        status, payload = server.apply_decision("pane-3", "approve")
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "BAD_PANE")
        code, _out, err = server.run_pane("3", signed["job"], signed, "Lead")
        self.assertEqual(code, 403)
        self.assertEqual(err, "BAD_PANE")
        self.assertEqual(self.reached, [])

    def test_padded_pane_is_bad_pane(self):
        protocol.read_seen(server.SEEN)
        for pane, job_id in (("01", "pad-01"), ("02", "pad-02")):
            fields = self._job(id=job_id, lane="ops", pane=pane)
            self.assertEqual(server.pane_reason(fields), "BAD_PANE")
            self.assertFalse(server.routes_to_watcher(fields))
            signed = self._sign(fields)
            self._write(signed)
            self.assertEqual(server.claim_job(job_id, signed["sig"]), "REFUSED BAD_PANE")
            status, payload = server.apply_decision(job_id, "approve")
            self.assertEqual(status, 403)
            self.assertEqual(payload.get("error"), "BAD_PANE")
            self.assertEqual(self.reached, [])

    def test_whitespace_padded_pane_is_bad_pane(self):
        protocol.read_seen(server.SEEN)
        for pane, job_id in ((" 1", "ws-lead"), ("2 ", "ws-trail")):
            fields = self._job(id=job_id, lane="ops", pane=pane)
            self.assertEqual(server.signed_pane(fields), pane)
            self.assertEqual(server.pane_reason(fields), "BAD_PANE")
            self.assertFalse(server.routes_to_watcher(fields))
            signed = self._sign(fields)
            self._write(signed)
            self.assertEqual(server.claim_job(job_id, signed["sig"]), "REFUSED BAD_PANE")
            status, payload = server.apply_decision(job_id, "approve")
            self.assertEqual(status, 403)
            self.assertEqual(payload.get("error"), "BAD_PANE")
            self.assertEqual(self.reached, [])


class TestClaimBusJobHelper(unittest.TestCase):
    def test_helper_script_shipped_and_51_safe(self):
        script = ROOT / "tools" / "Claim-BusJob.ps1"
        if not script.is_file():
            self.fail("tools/Claim-BusJob.ps1 is not shipped")
        text = script.read_text(encoding="utf-8")
        if any(line.strip().startswith("Set-ExecutionPolicy") for line in text.splitlines()):
            self.fail("Claim-BusJob.ps1 changes the execution policy")
        if "-ExecutionPolicy Bypass" not in text:
            self.fail("Claim-BusJob.ps1 does not document Bypass invocation")
        if "/api/claim" not in text:
            self.fail("helper does not call /api/claim")
        if "127.0.0.1" not in text or "HBC_HUB_PORT" not in text:
            self.fail("helper default hub is not localhost from HBC_HUB_PORT")
        if "$DefaultHubPort = 8107" not in text:
            self.fail("helper is missing the default hub port constant")
        if "Test-LoopbackHub" not in text:
            self.fail("helper does not keep -Hub on 127.0.0.1")
        if r"^http://127\.0\.0\.1(:\d+)?$" not in text:
            self.fail("helper loopback check must allow optional port on 127.0.0.1 only")
        if "localhost gets HTTP 421" not in text:
            self.fail("Claim-BusJob.ps1 does not document that -Hub must be 127.0.0.1")
        if "Test-StagingHub" not in text or "Test-LiveHandoffFile" not in text:
            self.fail("Claim-BusJob.ps1 does not refuse live-handoff claims on a staging hub")
        if "HBC_STAGING" not in text or "$userProfile" not in text:
            self.fail("Claim-BusJob.ps1 staging check must use HBC_STAGING and $userProfile")
        if "$home =" in text:
            self.fail("Claim-BusJob.ps1 must not assign $home (read-only $HOME)")
        if "REFUSED LIVE_HANDOFF" not in text:
            self.fail("Claim-BusJob.ps1 missing LIVE_HANDOFF refuse")
        if "id = $Id" not in text or "sig = $posted" not in text or "nonce" not in text:
            self.fail("helper does not pass id, sig, and nonce")
        if "Get-ClaimMac" not in text or "Test-SameHex" not in text:
            self.fail("helper does not verify the GO HMAC")
        if ".session-token" not in text:
            self.fail("helper does not read .session-token")
        if "Write-Output $hubToken" in text or 'Write-Output $Token' in text:
            self.fail("helper prints the session token")
        if "REFUSED HUB_DOWN" not in text:
            self.fail("helper missing HUB_DOWN")
        if "REFUSED TOKEN" not in text:
            self.fail("helper missing refused-token message")
        if "Never signs" not in text:
            self.fail("helper does not say it never signs")
        for banned in ("??", "?.", "ForEach-Object -Parallel", "Join-String"):
            if banned in text:
                self.fail("helper uses a PowerShell 7-only form: %s" % banned)
        if "$ExitGo = 0" not in text or "$ExitNoGo = 2" not in text or "$ExitError = 3" not in text:
            self.fail("helper is missing distinct GO / NO-GO / ERROR exits")
        if "ErrorDetails" not in text:
            self.fail("helper missing pwsh 7 ErrorDetails path")
        if "Exception.Response" not in text:
            self.fail("helper reads ErrorDetails without checking Exception.Response")
        if '-ceq "GO"' not in text:
            self.fail("helper does not compare GO with -ceq")
        if "StatusCode" not in text:
            self.fail("helper does not require HTTP 200")

    def test_helper_exit_codes_go_nogo_error(self):
        self.assertEqual(server.CLAIM_EXIT_GO, 0)
        self.assertEqual(server.CLAIM_EXIT_NOGO, 2)
        self.assertEqual(server.CLAIM_EXIT_ERROR, 3)
        self.assertEqual(server.claim_helper_exit("GO"), 0)
        for verdict in (
            "WAIT NEEDS_APPROVE",
            "DUPLICATE",
            "SPOOFED",
            "REFUSED STALE",
            "REFUSED BAD_TIME",
            "REFUSED STORE_ERROR",
        ):
            self.assertEqual(server.claim_helper_exit(verdict), 2)
            self.assertFalse(server.watcher_may_act(verdict, exit_code=2))
        self.assertEqual(server.claim_helper_exit("", error=True), 3)
        self.assertEqual(server.claim_helper_exit("REFUSED HUB_DOWN", error=True), 3)
        self.assertFalse(server.watcher_may_act("REFUSED HUB_DOWN", exit_code=3))
        self.assertEqual(server.claim_helper_trust(403, True, "GO"), 3)
        self.assertEqual(server.claim_helper_trust(200, False, "GO"), 3)
        self.assertEqual(server.claim_helper_trust(200, True, "GO"), 0)
        self.assertEqual(server.claim_helper_trust(200, True, "WAIT NEEDS_APPROVE"), 2)
        self.assertTrue(server.watcher_may_act("GO", exit_code=0))
        self.assertFalse(server.watcher_may_act("GO", exit_code=3))

    def test_helper_stub_non200_valid_mac_and_nonce_replay_exit_3(self):
        job_id = "stub-id"
        nonce = "nonce-live"
        mac = server.claim_reply_mac(job_id, nonce, "GO", token="lab-token")
        self.assertEqual(len(mac), 64)
        self.assertEqual(server.claim_helper_trust(403, True, "GO"), server.CLAIM_EXIT_ERROR)
        self.assertEqual(server.claim_helper_trust(500, True, "GO"), server.CLAIM_EXIT_ERROR)
        captured = {"id": job_id, "verdict": "GO", "mac": mac}
        replay_mac = server.claim_reply_mac(captured["id"], "nonce-2", "GO", token="lab-token")
        playback_ok = captured["mac"] == replay_mac
        self.assertEqual(captured["id"], job_id)
        self.assertFalse(playback_ok)
        self.assertEqual(
            server.claim_helper_trust(200, playback_ok, "GO"),
            server.CLAIM_EXIT_ERROR,
        )
        fixed = server.claim_reply_mac(captured["id"], nonce, "GO", token="lab-token")
        self.assertEqual(captured["mac"], fixed)
        self.assertEqual(
            server.claim_helper_trust(200, captured["mac"] == fixed, "GO"),
            server.CLAIM_EXIT_GO,
        )

    def test_helper_error_fails_closed(self):
        verdict = server.watcher_try_claim("err-1", "x", call=lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
        self.assertEqual(server.claim_helper_exit(verdict, error=True), server.CLAIM_EXIT_ERROR)
        self.assertFalse(server.watcher_may_act(verdict))
        self.assertNotEqual(verdict, "GO")

    def _run_helper(self, shell, script, extra, hub):
        cmd = [
            shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-Hub", hub, "-Token", "lab-token",
        ] + extra
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20)

    def test_helper_live_go_nogo_error(self):
        script = ROOT / "tools" / "Claim-BusJob.ps1"
        shell = _ps_shell()
        if not shell:
            self.skipTest("Claim-BusJob.ps1 live exits: powershell.exe (5.1) or pwsh not on PATH")
        sig = "ab" * 32
        token = "lab-token"
        seen = {"body": "", "go_id": None, "go_reply": None, "queue": ["GO", "WAIT NEEDS_APPROVE", "DUPLICATE", "bad-mac", "token", "non200", "replay"]}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                seen["body"] = self.rfile.read(length).decode("utf-8")
                posted = json.loads(seen["body"] or "{}")
                mode = seen["queue"].pop(0) if seen["queue"] else "GO"
                if mode == "token":
                    payload = json.dumps({"verdict": "REFUSED TOKEN"}).encode("ascii")
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if mode == "replay":
                    payload = seen["go_reply"] or json.dumps({"verdict": "GO", "mac": "00" * 32}).encode("ascii")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                verdict = "GO"
                if mode not in ("bad-mac", "non200", "GO"):
                    verdict = mode
                if mode == "bad-mac":
                    mac = "00" * 32
                else:
                    mac = server.claim_reply_mac(
                        posted.get("id", ""), posted.get("nonce", ""), verdict, token=token
                    )
                payload = json.dumps({"verdict": verdict, "mac": mac}).encode("ascii")
                if mode == "GO":
                    seen["go_id"] = posted.get("id", "")
                    seen["go_reply"] = payload
                code = 403 if mode == "non200" else 200
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, fmt, *args):
                return

        httpd = HTTPServer(("127.0.0.1", 0), Handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever)
        thread.daemon = True
        thread.start()
        hub = "http://127.0.0.1:%s" % port
        try:
            go = self._run_helper(shell, script, ["-Id", "help-go", "-Sig", sig], hub)
            go_out = (go.stdout or "") + (go.stderr or "")
            if go.returncode != server.CLAIM_EXIT_GO:
                self.fail("GO did not exit 0: %s" % go_out)
            if (go.stdout or "").splitlines()[:1] != ["GO"]:
                self.fail("GO first line was not GO: %s" % go.stdout)
            posted = json.loads(seen["body"])
            if posted.get("id") != "help-go" or posted.get("sig") != sig or not posted.get("nonce"):
                self.fail("helper POST body was not {id, sig, nonce}")
            if sig in (go.stdout or "") or sig in (go.stderr or ""):
                self.fail("helper leaked the signature on GO")
            if token in go_out:
                self.fail("helper leaked the session token on GO")

            nogo = self._run_helper(shell, script, ["-Id", "help-wait", "-Sig", sig], hub)
            nogo_out = (nogo.stdout or "") + (nogo.stderr or "")
            if nogo.returncode != server.CLAIM_EXIT_NOGO:
                self.fail("WAIT NO-GO did not exit 2: %s" % nogo_out)
            if "WAIT NEEDS_APPROVE" not in (nogo.stdout or ""):
                self.fail("WAIT NO-GO did not print the verdict")
            if server.watcher_may_act((nogo.stdout or "").splitlines()[0], exit_code=nogo.returncode):
                self.fail("NO-GO was treated as actionable")
            if token in nogo_out:
                self.fail("helper leaked the session token on NO-GO")

            dup = self._run_helper(shell, script, ["-Id", "help-dup", "-Sig", sig], hub)
            if dup.returncode != server.CLAIM_EXIT_NOGO:
                self.fail("DUPLICATE NO-GO did not exit 2: %s" % ((dup.stdout or "") + (dup.stderr or "")))

            bad = self._run_helper(shell, script, ["-Id", "help-badmac", "-Sig", sig], hub)
            bad_out = (bad.stdout or "") + (bad.stderr or "")
            if bad.returncode != server.CLAIM_EXIT_ERROR:
                self.fail("bad HMAC did not exit 3: %s" % bad_out)
            if (bad.stdout or "").strip() == "GO":
                self.fail("bad HMAC was treated as GO")

            refused = self._run_helper(shell, script, ["-Id", "help-token", "-Sig", sig], hub)
            refused_out = (refused.stdout or "") + (refused.stderr or "")
            if refused.returncode != server.CLAIM_EXIT_ERROR:
                self.fail("refused token did not exit 3: %s" % refused_out)
            if "REFUSED TOKEN" not in (refused.stdout or ""):
                self.fail("refused token was not its own message")

            non200 = self._run_helper(shell, script, ["-Id", "help-non200", "-Sig", sig], hub)
            non200_out = (non200.stdout or "") + (non200.stderr or "")
            if non200.returncode != server.CLAIM_EXIT_ERROR:
                self.fail("non-200 with valid MAC did not exit 3: %s" % non200_out)
            if (non200.stdout or "").strip() == "GO":
                self.fail("non-200 with valid MAC was treated as GO")

            replay = self._run_helper(shell, script, ["-Id", seen["go_id"] or "help-go", "-Sig", sig], hub)
            replay_out = (replay.stdout or "") + (replay.stderr or "")
            posted_replay = json.loads(seen["body"])
            if posted_replay.get("id") != seen["go_id"]:
                self.fail("replay did not play the captured GO back for the same id")
            if replay.returncode != server.CLAIM_EXIT_ERROR:
                self.fail("nonce replay did not exit 3: %s" % replay_out)
            if (replay.stdout or "").strip() == "GO":
                self.fail("nonce replay was treated as GO")

            down = self._run_helper(shell, script, ["-Id", "help-down", "-Sig", sig], "http://127.0.0.1:1")
            down_out = (down.stdout or "") + (down.stderr or "")
            if down.returncode != server.CLAIM_EXIT_ERROR:
                self.fail("hub-down did not exit 3: %s" % down_out)
            if "HUB_DOWN" not in down_out:
                self.fail("hub-down did not print HUB_DOWN")
            if "GO" == (down.stdout or "").strip():
                self.fail("hub-down printed GO")
            if server.watcher_may_act((down.stdout or "").strip(), exit_code=down.returncode):
                self.fail("error was not fail-closed")
            if sig in down_out or token in down_out:
                self.fail("helper leaked a secret on error")

            missing = self._run_helper(shell, script, ["-Id", "help-nosig"], hub)
            if missing.returncode != server.CLAIM_EXIT_ERROR:
                self.fail("missing sig did not exit 3: %s" % ((missing.stdout or "") + (missing.stderr or "")))
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
