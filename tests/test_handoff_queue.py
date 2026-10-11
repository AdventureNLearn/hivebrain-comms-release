"""Handoff-folder watcher jobs appear in queue/needs_you and approve-then-claim."""

import asyncio
import inspect
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from hub import protocol, sigverify
import server
from tests.live_guard import install_live_state_guard

install_live_state_guard()

WRITE_ORDER = (
    "id", "v", "from", "to", "lane", "priority", "time", "routed",
    "pane", "job", "done-when", "sig",
)
ET = protocol.ET


def _iso():
    return datetime.now(ET).replace(microsecond=0).isoformat()


class TestHandoffQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.bus = self.home / "bus"
        self.handoff = self.home / "handoff"
        self.bus.mkdir()
        self.handoff.mkdir()
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
        self.reached = []
        self._bus = server.BUS
        self._handoff = server.HANDOFF
        self._log = server.LOG
        self._seen = server.SEEN
        self._sessions = server.SESSIONS
        self._events = getattr(server, "EVENTS", None)
        server.BUS = self.bus
        server.HANDOFF = self.handoff
        server.LOG = self.home / "approvals.jsonl"
        server.SEEN = self.home / ".seen-ids.txt"
        server.SESSIONS = self.home / ".sessions.json"
        if self._events is not None:
            server.EVENTS = self.home / ".hub-events.jsonl"
        server._logged_spoofed.clear()
        server._seen_alarm_state = None
        server._seen_claim_alarmed = False
        if hasattr(server, "_shadow_alarmed"):
            server._shadow_alarmed.clear()
        if hasattr(server, "_bus_read_alarmed"):
            server._bus_read_alarmed.clear()
        server._preview_quiet = False
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
        server.HANDOFF = self._handoff
        server.LOG = self._log
        server.SEEN = self._seen
        server.SESSIONS = self._sessions
        if self._events is not None:
            server.EVENTS = self._events
        server._preview_quiet = False
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
        out = dict(public)
        out["sig"] = mac
        return out

    def _write(self, fields, folder, name="LEAD-TO-BUILD.md"):
        public = protocol.public_fields(fields)
        lines = [f"{key}: {public[key]}" for key in WRITE_ORDER if key in public]
        (folder / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_01_handoff_job_appears_in_needs_you_and_queue(self):
        signed = self._sign(self._job(id="handoff-1"))
        self._write(signed, self.handoff)
        protocol.read_seen(server.SEEN)
        needs = server.needs_you()
        ids = [row.get("id") for row in needs]
        self.assertNotIn("handoff-1", ids)
        qids = [item.get("id") for item in server.queue()]
        self.assertIn("handoff-1", qids)
        watcher = [item for item in server.queue() if item.get("id") == "handoff-1"][0]
        self.assertEqual(watcher.get("path"), "watcher")
        self.assertEqual(watcher.get("status"), "ready")
        self.assertNotIn(signed["sig"], str(needs))
        self.assertEqual(server.claim_job("handoff-1", signed["sig"]), "GO")

    def test_02_approve_then_claim_go_for_that_id_only(self):
        one = self._sign(self._job(id="only-go", job="do the first handoff job"))
        two = self._sign(self._job(id="still-wait", job="do the second handoff job"))
        blocks = []
        for fields in (one, two):
            public = protocol.public_fields(fields)
            blocks.append("\n".join(f"{key}: {public[key]}" for key in WRITE_ORDER if key in public))
        (self.handoff / "LEAD-TO-BUILD.md").write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
        protocol.read_seen(server.SEEN)
        status, payload = server.apply_decision("only-go", "approve")
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("result"), "approved")
        self.assertEqual(self.reached, [])
        self.assertEqual(server.claim_job("only-go", one["sig"]), "GO")
        self.assertEqual(server.claim_job("still-wait", two["sig"]), "GO")
        if not protocol.find_seen(server.SEEN, "still-wait"):
            self.fail("ready signed id did not enter the seen-list")

    def test_03_tampered_sig_refused(self):
        signed = self._sign(self._job(id="bad-sig-h"))
        self._write(signed, self.handoff)
        protocol.read_seen(server.SEEN)
        server.record_approval("bad-sig-h", "2", fields=signed)
        verdict = server.claim_job("bad-sig-h", "0" * 64)
        self.assertEqual(verdict, "SPOOFED")
        if protocol.find_seen(server.SEEN, "bad-sig-h"):
            self.fail("tampered sig entered the seen-list")
        self.assertEqual(self.reached, [])

    def test_04_lab_bus_job_unaffected(self):
        lab = self._sign(self._job(id="lab-bus", lane="lab", pane="1", to="Build"))
        self._write(lab, self.bus)
        protocol.read_seen(server.SEEN)
        qids = [item.get("id") for item in server.queue()]
        self.assertIn("lab-bus", qids)
        lab_row = [item for item in server.queue() if item.get("id") == "lab-bus"][0]
        self.assertEqual(lab_row.get("path"), "hub")
        needs_ids = [row.get("id") for row in server.needs_you()]
        self.assertNotIn("lab-bus", needs_ids)
        self.assertEqual(server.claim_job("lab-bus", lab["sig"]), "REFUSED WRONG_PATH")
        status, payload = server.apply_decision("lab-bus", "approve")
        self.assertEqual(status, 200)
        self.assertEqual(self.reached, ["1"])

    def test_05_replay_refused(self):
        signed = self._sign(self._job(id="replay-h"))
        self._write(signed, self.handoff)
        protocol.read_seen(server.SEEN)
        status, payload = server.apply_decision("replay-h", "approve")
        self.assertEqual(status, 200)
        self.assertEqual(server.claim_job("replay-h", signed["sig"]), "GO")
        self.assertEqual(server.claim_job("replay-h", signed["sig"]), "DUPLICATE")
        self.assertEqual(self.reached, [])

    def test_06_approve_helper_is_51_safe(self):
        script = ROOT / "tools" / "Approve-BusJob.ps1"
        self.assertTrue(script.is_file())
        text = script.read_text(encoding="utf-8")
        self.assertIn("/api/decide", text)
        self.assertIn("127.0.0.1", text)
        self.assertIn("HBC_HUB_PORT", text)
        self.assertIn("$DefaultHubPort = 8107", text)
        self.assertIn("Get-HubBase", text)
        self.assertIn("localhost gets HTTP 421", text)
        self.assertIn("Never signs", text)
        self.assertNotIn("Write-Output $hubToken", text)
        self.assertNotIn("Write-Output $Token", text)
        for banned in ("??", "?.", "ForEach-Object -Parallel", "Join-String"):
            self.assertNotIn(banned, text)
        self.assertIn("/api/job", text)
        self.assertIn("IsInputRedirected", text)
        self.assertIn("UserInteractive", text)
        self.assertIn("X-Hbc-Token", text)
        self.assertIn("hash = $job.hash", text)
        self.assertIn('"hash: "', text)
        self.assertNotIn("USERPROFILE", text)
        self.assertNotIn("Get-JobBlock", text)
        desk = (ROOT / "desk.js").read_text(encoding="utf-8")
        self.assertIn("JSON.stringify({ id, decision, hash })", desk)
        self.assertIn("decide(job.id, decision, job.hash)", desk)
        self.assertNotIn("body.get(\"sig\")", inspect.getsource(server.decide))
        self.assertIn("HASH_REQUIRED", inspect.getsource(server.decide))
        self.assertIn("body.get(\"hash\")", inspect.getsource(server.decide))

    def test_07_handoff_lab_pane_never_runs(self):
        lab = self._sign(self._job(id="lab-hand", lane="lab", pane="1", to="Build"))
        self._write(lab, self.handoff)
        protocol.read_seen(server.SEEN)
        fields, owner, source = server.find_job("lab-hand")
        self.assertEqual(source, server.SOURCE_HANDOFF)
        self.assertFalse(server.routes_to_watcher(fields))
        self.assertFalse(server.record_approval("lab-hand", "1", fields=lab))
        status, payload = server.apply_decision("lab-hand", "approve")
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "WRONG_PATH")
        self.assertEqual(server.claim_job("lab-hand", lab["sig"]), "REFUSED WRONG_PATH")
        qids = [item.get("id") for item in server.queue()]
        self.assertNotIn("lab-hand", qids)
        needs_ids = [row.get("id") for row in server.needs_you()]
        self.assertNotIn("lab-hand", needs_ids)
        self.assertEqual(self.reached, [])

    def test_08_shadow_dup_refused_everywhere(self):
        signed = self._sign(self._job(id="dup"))
        self._write(signed, self.bus)
        self._write(signed, self.handoff)
        protocol.read_seen(server.SEEN)
        fields, owner, source = server.find_job("dup")
        self.assertEqual(source, server.SOURCE_SHADOW)
        self.assertIsNotNone(fields)
        status, payload = server.apply_decision("dup", "approve")
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "SHADOW")
        self.assertEqual(server.claim_job("dup", signed["sig"]), "REFUSED SHADOW")
        self.assertFalse(server.record_approval("dup", "2", fields=signed))
        preview_status, preview = server.job_preview_payload("dup")
        self.assertEqual(preview_status, 403)
        self.assertEqual(preview.get("error"), "SHADOW")
        held = [item for item in server.queue() if item.get("id") == "dup"]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0].get("status"), "held")
        self.assertEqual(held[0].get("reason"), "SHADOW")
        self.assertEqual(held[0].get("path"), "shadow")
        needs_ids = [row.get("id") for row in server.needs_you()]
        self.assertNotIn("dup", needs_ids)
        self.assertEqual(self.reached, [])
        rows = []
        if server.LOG.is_file():
            for line in server.LOG.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        alarms = [row for row in rows if row.get("reason") == "SHADOW"]
        self.assertEqual(len(alarms), 1)

    def test_09_second_matching_approve_is_duplicate(self):
        signed = self._sign(self._job(id="dup-appr"))
        self._write(signed, self.handoff)
        protocol.read_seen(server.SEEN)
        status, payload = server.apply_decision("dup-appr", "approve")
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("result"), "approved")
        status2, payload2 = server.apply_decision("dup-appr", "approve")
        self.assertEqual(status2, 403)
        self.assertEqual(payload2.get("error"), "DUPLICATE")
        self.assertEqual(self.reached, [])

    def _decision_rows(self, job_id):
        rows = []
        if not server.LOG.is_file():
            return rows
        for line in server.LOG.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("id") == job_id and row.get("decision") in ("approve", "deny"):
                rows.append(row)
        return rows

    def test_09b_second_approve_duplicate_no_new_record(self):
        signed = self._sign(self._job(id="final-appr"))
        self._write(signed, self.handoff)
        protocol.read_seen(server.SEEN)
        status, payload = server.apply_decision("final-appr", "approve")
        self.assertEqual(status, 200)
        before = self._decision_rows("final-appr")
        self.assertEqual(len(before), 1)
        status2, payload2 = server.apply_decision("final-appr", "approve")
        self.assertEqual(status2, 403)
        self.assertEqual(payload2.get("error"), "DUPLICATE")
        self.assertEqual(self._decision_rows("final-appr"), before)
        build = (ROOT / "build.html").read_text(encoding="utf-8")
        self.assertNotIn("async function decide", build)
        src = Path(server.__file__).read_text(encoding="utf-8")
        apply = src.split("def _apply_decision", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("last_hub_decision", apply)
        self.assertIn("routes_to_watcher(fields) or source == SOURCE_HANDOFF", apply)
        self.assertLess(apply.find("last_hub_decision"), apply.find("evaluate("))
        self.assertIn("Hub-pane bus jobs are already single-run", apply)

    def test_09c_deny_after_approve_already_decided_no_record(self):
        signed = self._sign(self._job(id="appr-then-deny"))
        self._write(signed, self.handoff)
        protocol.read_seen(server.SEEN)
        status, payload = server.apply_decision("appr-then-deny", "approve")
        self.assertEqual(status, 200)
        before = self._decision_rows("appr-then-deny")
        self.assertEqual([row.get("decision") for row in before], ["approve"])
        status2, payload2 = server.apply_decision("appr-then-deny", "deny")
        self.assertEqual(status2, 403)
        self.assertEqual(payload2.get("error"), "ALREADY_DECIDED")
        self.assertEqual(self._decision_rows("appr-then-deny"), before)

    def test_09d_approve_after_deny_already_decided_no_record(self):
        signed = self._sign(self._job(id="deny-then-appr"))
        self._write(signed, self.handoff)
        protocol.read_seen(server.SEEN)
        status, payload = server.apply_decision("deny-then-appr", "deny")
        self.assertEqual(status, 200)
        before = self._decision_rows("deny-then-appr")
        self.assertEqual([row.get("decision") for row in before], ["deny"])
        status2, payload2 = server.apply_decision("deny-then-appr", "approve")
        self.assertEqual(status2, 403)
        self.assertEqual(payload2.get("error"), "ALREADY_DECIDED")
        self.assertEqual(self._decision_rows("deny-then-appr"), before)

    def test_10_handoff_path_and_read_bus_cap(self):
        ok, resolved = server._handoff_path_ok(self.handoff)
        self.assertTrue(ok)
        self.assertEqual(Path(resolved), self.handoff.resolve())
        self.assertFalse(server._handoff_path_ok("handoff")[0])
        self.assertFalse(server._handoff_path_ok("./handoff")[0])
        self.assertFalse(server._handoff_path_ok(r"\\server\share\handoff")[0])
        self.assertFalse(server._handoff_path_ok("//server/share/handoff")[0])
        link = self.home / "hand-link"
        try:
            link.symlink_to(self.handoff, target_is_directory=True)
        except (OSError, NotImplementedError):
            link = None
        if link is not None and link.exists():
            self.assertFalse(server._handoff_path_ok(link)[0])
        prev = server.HANDOFF
        try:
            self.assertIsNotNone(server.configure_handoff(self.handoff))
            self.assertIsNone(server.configure_handoff("relative-handoff"))
            self.assertIsNone(server.HANDOFF)
        finally:
            server.HANDOFF = prev
        oversized = self.bus / "LEAD-TO-BUILD.md"
        oversized.write_bytes(b"x" * (server.BUS_READ_MAX_BYTES + 1))
        self.assertEqual(server.read_bus(oversized), "")
        rows = []
        if server.LOG.is_file():
            for line in server.LOG.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        self.assertTrue(any(row.get("reason") == "BUS_READ" for row in rows))

    def test_11_job_preview_no_raw_sig(self):
        signed = self._sign(self._job(id="prev-1", job="preview this watcher job"))
        self._write(signed, self.handoff)
        protocol.read_seen(server.SEEN)
        status, payload = server.job_preview_payload("prev-1")
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("id"), "prev-1")
        self.assertEqual(payload.get("from"), "Lead")
        self.assertEqual(payload.get("lane"), "ops")
        self.assertEqual(payload.get("summary"), "preview this watcher job")
        self.assertEqual(payload.get("bind"), server.approval_bind(signed))
        self.assertEqual(payload.get("hash"), protocol.content_fingerprint(signed))
        self.assertNotIn("sig", payload)
        self.assertNotIn(signed["sig"], str(payload))
        missing, miss_payload = server.job_preview_payload("no-such-job")
        self.assertEqual(missing, 404)
        self.assertEqual(miss_payload.get("error"), "unknown job")

    def _tree_snap(self, root):
        found = {}
        for path in sorted(Path(root).rglob("*")):
            if path.is_file():
                found[str(path.relative_to(root))] = path.read_bytes()
        return found

    def test_12_preview_writes_nothing(self):
        signed = self._sign(self._job(id="snap-1"))
        self._write(signed, self.handoff)
        (self.bus / "GROKBOT-TO-BUILD.md").write_bytes(b"x" * (server.BUS_READ_MAX_BYTES + 1))
        protocol.read_seen(server.SEEN)
        before = self._tree_snap(self.home)
        status, payload = server.job_preview_payload("snap-1")
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("id"), "snap-1")
        self.assertEqual(self._tree_snap(self.home), before)
        self.assertFalse(server.LOG.is_file())
        shadowed = self._sign(self._job(id="snap-dup", job="shadow preview job"))
        self._write(shadowed, self.bus)
        self._write(shadowed, self.handoff)
        before_dup = self._tree_snap(self.home)
        shadow_status, _shadow = server.job_preview_payload("snap-dup")
        self.assertEqual(shadow_status, 403)
        self.assertEqual(self._tree_snap(self.home), before_dup)
        self.assertFalse(server.LOG.is_file())
        self.assertFalse(server._preview_quiet)
        self.assertEqual(self.reached, [])

    def test_13_stale_view_hash_is_409(self):
        signed = self._sign(self._job(id="stale-h"))
        self._write(signed, self.handoff)
        protocol.read_seen(server.SEEN)
        preview_status, preview = server.job_preview_payload("stale-h")
        self.assertEqual(preview_status, 200)
        shown = preview.get("hash")
        self.assertEqual(shown, protocol.content_fingerprint(signed))
        qhash = [item.get("hash") for item in server.queue() if item.get("id") == "stale-h"]
        self.assertEqual(qhash, [shown])
        status, payload = server.apply_decision("stale-h", "approve", view_hash="0" * 64)
        self.assertEqual(status, 409)
        self.assertEqual(payload.get("error"), "STALE")
        self.assertEqual(self.reached, [])
        status, payload = server.apply_decision("stale-h", "approve", view_hash=shown)
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("result"), "approved")
        self.assertEqual(self.reached, [])

    def test_14_decide_archived_or_returned_is_duplicate(self):
        returned = self._sign(self._job(id="ret-1"))
        self._write(returned, self.handoff)
        (self.bus / "BUILD-TO-LEAD.md").write_text("VERDICT: RAN\nid: ret-1\n", encoding="utf-8")
        protocol.read_seen(server.SEEN)
        status, payload = server.apply_decision("ret-1", "approve")
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "DUPLICATE")
        self.assertEqual(self.reached, [])

        archived = self._sign(self._job(id="arch-1", job="archive this watcher job"))
        live = self.bus / "LEAD-TO-BUILD.md"
        self._write(archived, self.bus)
        protocol.move_finished_to_archive(live, {"arch-1"})
        protocol.read_seen(server.SEEN)
        self.assertIn("arch-1", server.archived_job_ids())
        status, payload = server.apply_decision("arch-1", "approve")
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "DUPLICATE")
        self.assertEqual(self.reached, [])

    def test_15_apply_decision_wrong_path(self):
        lab = self._sign(self._job(id="wp-1", lane="lab", pane="1", to="Build"))
        self._write(lab, self.handoff)
        protocol.read_seen(server.SEEN)
        with mock.patch.object(server, "record_approval", return_value=True):
            status, payload = server._apply_decision("wp-1", "approve")
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "WRONG_PATH")
        self.assertEqual(self.reached, [])
        self.assertFalse(server.is_approved("wp-1", lab))

    def _decide_http(self, body):
        prev = server.TOKEN
        server.TOKEN = "a" * 32

        class Req:
            path = "/api/decide"
            query = {}
            headers = {"X-Hbc-Token": server.TOKEN}

            async def json(self):
                return body

        async def run():
            resp = await server.decide(Req())
            raw = resp.body
            if isinstance(raw, (bytes, bytearray)):
                payload = json.loads(raw.decode("utf-8"))
            else:
                payload = json.loads(raw)
            return resp.status, payload

        try:
            return asyncio.run(run())
        finally:
            server.TOKEN = prev

    def test_16_decide_requires_posted_hash(self):
        signed = self._sign(self._job(id="hash-req"))
        self._write(signed, self.handoff)
        protocol.read_seen(server.SEEN)
        shown = protocol.content_fingerprint(signed)
        missing, miss_payload = self._decide_http({"id": "hash-req", "decision": "approve"})
        self.assertEqual(missing, 400)
        self.assertEqual(miss_payload.get("error"), "HASH_REQUIRED")
        via_sig, sig_payload = self._decide_http({
            "id": "hash-req",
            "decision": "approve",
            "sig": shown,
        })
        self.assertEqual(via_sig, 400)
        self.assertEqual(sig_payload.get("error"), "HASH_REQUIRED")
        stale, stale_payload = self._decide_http({
            "id": "hash-req",
            "decision": "approve",
            "hash": "0" * 64,
        })
        self.assertEqual(stale, 409)
        self.assertEqual(stale_payload.get("error"), "STALE")
        ok, ok_payload = self._decide_http({
            "id": "hash-req",
            "decision": "approve",
            "hash": shown,
        })
        self.assertEqual(ok, 200)
        self.assertEqual(ok_payload.get("result"), "approved")
        self.assertEqual(self.reached, [])
        src = inspect.getsource(server.decide)
        dropped = src.replace("view_hash=view_hash", "view_hash=\"\"")
        if "HASH_REQUIRED" not in src or "view_hash=view_hash" not in src:
            self.fail("decide does not pass the posted hash")
        if "HASH_REQUIRED" in dropped and "view_hash=view_hash" in dropped:
            self.fail("hash-drop mutant still looks required")

    def test_17_spoofed_does_not_consume_id(self):
        forged = self._job(id="spoof-keep")
        self._write(forged, self.handoff)
        protocol.read_seen(server.SEEN)
        status, payload = server.apply_decision("spoof-keep", "approve")
        self.assertEqual(status, 403)
        self.assertTrue(str(payload.get("error") or "").startswith("SPOOFED"))
        if protocol.find_seen(server.SEEN, "spoof-keep"):
            self.fail("SPOOFED consumed the id")
        real = self._sign(self._job(id="spoof-keep"))
        self._write(real, self.handoff)
        status, payload = server.apply_decision("spoof-keep", "approve")
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("result"), "approved")
        self.assertEqual(self.reached, [])

    def test_18_read_bus_bytes_cap_mutant(self):
        src = inspect.getsource(server.read_bus)
        self.assertIn("read(BUS_READ_MAX_BYTES + 1)", src)
        self.assertIn('"rb"', src)
        self.assertNotIn("newline", src)
        plain = src.replace("handle.read(BUS_READ_MAX_BYTES + 1)", "handle.read()")
        if "read(BUS_READ_MAX_BYTES + 1)" in plain:
            self.fail("plain read() mutant still has the cap")
        oversized = self.bus / "LEAD-TO-BUILD.md"
        oversized.write_bytes(b"x" * (server.BUS_READ_MAX_BYTES + 1))
        self.assertEqual(server.read_bus(oversized), "")

    def test_19_preview_record_quiet_guard(self):
        src = inspect.getsource(server.record)
        self.assertIn("_preview_quiet", src)
        stripped = "\n".join(
            line for line in src.splitlines() if "_preview_quiet" not in line
        )
        if "if _preview_quiet" in stripped:
            self.fail("quiet-guard mutant still present")
        signed = self._sign(self._job(id="quiet-1"))
        self._write(signed, self.handoff)
        (self.bus / "GROKBOT-TO-BUILD.md").write_bytes(b"x" * (server.BUS_READ_MAX_BYTES + 1))
        protocol.read_seen(server.SEEN)
        before = self._tree_snap(self.home)
        status, payload = server.job_preview_payload("quiet-1")
        self.assertEqual(status, 200)
        self.assertEqual(self._tree_snap(self.home), before)
        self.assertFalse(server.LOG.is_file())

    def test_20_ascii_id_vs_cyrillic_o(self):
        ascii_id = "job-1"
        cyrillic_id = "j\u043eb-1"
        self.assertNotEqual(ascii_id, cyrillic_id)
        bus_job = self._sign(self._job(id=ascii_id, lane="lab", pane="1"))
        hand = self._sign(self._job(id=cyrillic_id, job="cyrillic lookalike"))
        self._write(bus_job, self.bus)
        self._write(hand, self.handoff)
        protocol.read_seen(server.SEEN)
        fields, _owner, source = server.find_job(ascii_id)
        self.assertEqual(source, server.SOURCE_BUS)
        self.assertEqual(fields.get("id"), ascii_id)
        cyr_fields, _cyr_owner, cyr_source = server.find_job(cyrillic_id)
        self.assertEqual(cyr_source, server.SOURCE_HANDOFF)
        self.assertEqual(server.job_id_reason(cyr_fields), "BAD_ID")
        self.assertEqual(server.job_id_reason(fields), "")
        held = [item for item in server.queue() if item.get("id") == cyrillic_id]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0].get("status"), "held")
        self.assertEqual(held[0].get("reason"), "BAD_ID")
        ready = [item for item in server.queue() if item.get("id") == ascii_id]
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0].get("status"), "ready")
        status, payload = server.apply_decision(cyrillic_id, "approve")
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "BAD_ID")
        self.assertEqual(self.reached, [])
        status, payload = server.apply_decision(ascii_id, "approve")
        self.assertEqual(status, 200)
        self.assertEqual(self.reached, ["1"])

    def test_21_bad_id_each_layer_alone(self):
        cyr = self._job(id="j\u043eb-layer")
        signed = self._sign(cyr)
        protocol.read_seen(server.SEEN)
        self.assertIn("job_id_reason", inspect.getsource(server.check))
        self.assertEqual(server.check(signed, "Lead"), "BAD_ID")
        self.assertIn("job_id_reason", inspect.getsource(server.evaluate))
        unsigned = server.evaluate(cyr, "Lead")
        self.assertEqual(unsigned.kind, "REFUSED")
        self.assertEqual(unsigned.http, "BAD_ID")
        self.assertNotEqual(unsigned.kind, "SPOOFED")
        with mock.patch.object(server, "check", return_value=""):
            still = server.evaluate(signed, "Lead")
            self.assertEqual(still.http, "BAD_ID")
            self.assertFalse(still.ready)
            self.assertEqual(self.reached, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
