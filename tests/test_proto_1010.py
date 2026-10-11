"""SPEC-L-PROTO-1010 rev 6 probes (17). Temporary test keys only. Keys are deleted after each test."""

import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
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

WRITE_ORDER = (
    "id", "v", "from", "to", "lane", "priority", "time", "routed",
    "pane", "job", "done-when", "sig",
)
VERDICT_LINE = protocol.VERDICT_LINE
REQUIRED = list(sigverify.SIGNED_FIELDS)
ET = ZoneInfo("America/New_York")

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
    when = datetime.now(ET)
    if delta is not None:
        when = when + delta
    return when.replace(microsecond=0).isoformat()


class TestProto1010(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
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
        self.replies = []
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
            "lane": "lab",
            "priority": "normal",
            "time": iso,
            "routed": "Lead " + iso,
            "job": "list lab pane files",
            "done-when": "the pane lists its files",
            "pane": "1",
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

    def _write_raw(self, text, name="LEAD-TO-BUILD.md"):
        (self.bus / name).write_text(text, encoding="utf-8")

    def _return_text(self, owner="Lead"):
        path = self.bus / server.RETURNS[owner]
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")

    def _last_reply(self, owner="Lead"):
        text = protocol.last_reply(self._return_text(owner))
        self.replies.append(text)
        return text

    def _process(self, fields, name="LEAD-TO-BUILD.md"):
        self._write(fields, name)
        status, payload = server.apply_decision(fields.get("id", ""), "approve")
        reply = self._last_reply(server.OWNERS.get(name, "Lead"))
        self._assert_clean(reply)
        return status, payload, reply

    def _process_raw(self, text, job_id, name="LEAD-TO-BUILD.md"):
        self._write_raw(text, name)
        status, payload = server.apply_decision(job_id, "approve")
        reply = self._last_reply(server.OWNERS.get(name, "Lead"))
        self._assert_clean(reply)
        return status, payload, reply

    def _log_rows(self):
        if not server.LOG.is_file():
            return []
        rows = []
        for line in server.LOG.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def _line1(self, reply):
        lines = [line for line in (reply or "").splitlines() if line.strip()]
        if not lines:
            self.fail("reply is empty")
        return lines[0]

    def _assert_verdict_line(self, reply):
        line1 = self._line1(reply)
        if VERDICT_LINE.fullmatch(line1) is None:
            self.fail("reply line 1 is not a valid VERDICT line")
        return line1

    def _assert_not_run(self):
        if self.reached:
            self.fail("job was run")

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

    def _sign_raw_block(self, body, owner="Lead"):
        parsed = server.blocks(body)
        if not parsed:
            self.fail("raw job block did not parse")
        signed = self._sign(parsed[0], owner)
        return body.rstrip() + "\nsig: " + signed["sig"] + "\n"

    def _header(self):
        iso = _iso()
        return (
            "v: 2\nfrom: Lead\nto: Build\nlane: lab\n"
            "priority: normal\ntime: " + iso + "\n"
            "routed: Lead " + iso + "\npane: 1\n"
        )

    def test_prb_047a(self):
        body = (
            "id: prb-047a\n" + self._header()
            + "job: list lab pane files\n"
            + protocol.STOP_TOKEN
            + "\ndone-when: the pane lists its files\n"
        )
        text = self._sign_raw_block(body)
        status, payload, reply = self._process_raw(text, "prb-047a")
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: STOPPED")
        self._assert_not_run()

    def test_prb_047(self):
        """Original PRB-047: a stop line inside a job is not run."""
        self.test_prb_047a()

    def test_prb_047b(self):
        body = (
            "id: prb-047b\n" + self._header()
            + "job: list lab pane files\nstop: now\n"
            "done-when: the pane lists its files\n"
        )
        text = self._sign_raw_block(body)
        status, payload, reply = self._process_raw(text, "prb-047b")
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED MALFORMED_STOP")
        self._assert_not_run()

    def test_prb_047b_st0p(self):
        body = (
            "id: prb-047b-st0p\n" + self._header()
            + "job: list lab pane files\nST0P\n"
            "done-when: the pane lists its files\n"
        )
        text = self._sign_raw_block(body)
        status, payload, reply = self._process_raw(text, "prb-047b-st0p")
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED MALFORMED_STOP")
        self._assert_not_run()

    def test_prb_108(self):
        signed = self._sign(self._job(id="prb-108"))
        status1, _payload1, reply1 = self._process(signed)
        self.assertEqual(status1, 200)
        self.assertEqual(self._assert_verdict_line(reply1), "VERDICT: RAN")
        self.assertEqual(self.reached, ["1"])
        status2, _payload2, reply2 = self._process(signed)
        self.assertEqual(status2, 403)
        self.assertEqual(self._assert_verdict_line(reply2), "VERDICT: DUPLICATE")
        self.assertIn("original-id: prb-108", reply2)
        self.assertIn("original-verdict: RAN", reply2)
        self.assertEqual(self.reached, ["1"])

    def test_prb_110(self):
        first = self._sign(self._job(id="prb-110a"))
        status1, _payload1, reply1 = self._process(first)
        self.assertEqual(status1, 200)
        self.assertEqual(self._assert_verdict_line(reply1), "VERDICT: RAN")
        later = _iso(timedelta(minutes=1))
        second = self._sign(self._job(id="prb-110b", **{"time": later}))
        status2, _payload2, reply2 = self._process(second)
        self.assertEqual(status2, 403)
        self.assertEqual(self._assert_verdict_line(reply2), "VERDICT: DUPLICATE SAME_CONTENT")
        self.assertIn("original-id: prb-110a", reply2)
        self.assertIn("original-verdict: RAN", reply2)
        self.assertEqual(self.reached, ["1"])

    def test_prb_v1(self):
        cases = []
        signed_ok = self._sign(self._job(id="prb-v1-ok"))
        _status, _payload, reply_ok = self._process(signed_ok)
        cases.append(reply_ok)
        body = (
            "id: prb-v1-stop\n" + self._header()
            + "job: list lab pane files\n"
            + protocol.STOP_TOKEN
            + "\ndone-when: the pane lists its files\n"
        )
        _status, _payload, reply_stop = self._process_raw(self._sign_raw_block(body), "prb-v1-stop")
        cases.append(reply_stop)
        missing = self._sign(self._job(id="prb-v1-nr", routed=""))
        _status, _payload, reply_nr = self._process(missing)
        cases.append(reply_nr)
        high = self._sign(self._job(id="prb-v1-pri", priority="high"))
        _status, _payload, reply_pri = self._process(high)
        cases.append(reply_pri)
        for reply in cases:
            self._assert_verdict_line(reply)

    def test_prb_r1(self):
        signed = self._sign(self._job(id="prb-r1", routed=""))
        status, payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED NOT_ROUTED")
        self._assert_not_run()

    def test_prb_r2(self):
        iso = _iso()
        unknown = self._sign(self._job(id="prb-r2a", routed="Other " + iso))
        status, payload, reply = self._process(unknown)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED BAD_ROUTED")
        self._assert_not_run()
        future_routed = _iso(timedelta(hours=1))
        future = self._sign(self._job(id="prb-r2b", routed="Lead " + future_routed))
        status2, payload2, reply2 = self._process(future)
        self.assertEqual(status2, 403)
        self.assertEqual(self._assert_verdict_line(reply2), "VERDICT: REFUSED BAD_ROUTED")
        self._assert_not_run()
        no_router = self._sign(self._job(id="prb-r2c", routed=iso))
        status3, payload3, reply3 = self._process(no_router)
        self.assertEqual(status3, 403)
        self.assertEqual(self._assert_verdict_line(reply3), "VERDICT: REFUSED BAD_ROUTED")
        self._assert_not_run()

    def test_prb_r3(self):
        """Probe 16: routed: GB, ... is BAD_ROUTED on the hub and exit 3 on the signer."""
        iso = _iso()
        routed = "GB, " + iso
        self.assertEqual(protocol.ALLOWED_ROUTERS, ("Grok Bot", "Lead"))
        self.assertNotIn("GB", protocol.ALLOWED_ROUTERS)
        signed = self._sign(self._job(id="prb-r3", routed=routed))
        status, payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED BAD_ROUTED")
        self.assertEqual(payload.get("error"), "BAD_ROUTED")
        self._assert_not_run()

        shell = shutil.which("powershell") or shutil.which("pwsh")
        if not shell:
            self.skipTest("PRB-R3 signer: powershell.exe (5.1) or pwsh not on PATH")
        raw = self.home / "prb-r3-lead.key"
        raw.write_bytes(bytes(self.keys["Lead"]))
        job_file = self.home / "prb-r3.txt"
        job_file.write_text(
            "id: prb-r3s\nv: 2\nfrom: Lead\nto: Build\nlane: lab\npriority: normal\n"
            "time: %s\nrouted: %s\n"
            "job: list lab pane files\ndone-when: the pane lists its files\npane: 1\n"
            % (iso, routed),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                shell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(ROOT / "tools" / "Sign-BusJob.ps1"),
                "-Owner", "Lead", "-Id", "prb-r3s",
                "-TestKeyPath", str(raw), str(job_file),
            ],
            capture_output=True, text=True, timeout=20,
        )
        if completed.returncode != 3:
            self.fail("PRB-R3 signer did not exit 3: exit %s stdout=%s stderr=%s" % (
                completed.returncode, completed.stdout, completed.stderr,
            ))
        if "SIGNED" in (completed.stdout or ""):
            self.fail("PRB-R3 signer printed SIGNED for GB")

    def test_prb_p1(self):
        signed = self._sign(self._job(id="prb-p1", priority="high"))
        status, payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED BAD_PRIORITY")
        self.assertEqual(payload.get("error"), "BAD_PRIORITY")
        self._assert_not_run()
        both = self._sign(self._job(id="prb-p1-order", priority="high", routed=""))
        status2, payload2, reply2 = self._process(both)
        self.assertEqual(status2, 403)
        self.assertEqual(self._assert_verdict_line(reply2), "VERDICT: REFUSED BAD_PRIORITY")
        self.assertEqual(payload2.get("error"), "BAD_PRIORITY")
        self._assert_not_run()

    def test_prb_h1(self):
        text = (ROOT / "BUS-PROTOCOL.md").read_text(encoding="utf-8")
        first = text.splitlines()[0]
        if "BUS-PROTOCOL v2" not in first:
            self.fail("BUS-PROTOCOL.md header is not v2")
        if first.lower().startswith("# bus-protocol v1"):
            self.fail("BUS-PROTOCOL.md header is v1")
        for name in REQUIRED:
            token = "`%s`" % name
            if token not in text:
                self.fail("required-fields list is missing a signed field")
        if "`routed` is required" not in text and "routed` is required" not in text:
            self.fail("routed is not marked required")
        if "2026-10-10T15:05:00-04:00" not in text:
            self.fail("rules 11-13 do not quote the ISO form")
        if "ISO 8601" not in text and "ISO-8601" not in text:
            self.fail("ISO-8601 is not quoted")
        if "DENIED" not in text:
            self.fail("DENIED is not in the verdict list")
        if "no aliases" not in text:
            self.fail("BUS-PROTOCOL.md does not refuse GB aliases")
        if "P4c" not in text:
            self.fail("BUS-PROTOCOL.md is missing P4c")
        if "BAD_FIELD" not in text or "BAD_VERB" not in text:
            self.fail("BUS-PROTOCOL.md is missing P4c codes")
        if "hub/banned-verbs.txt" not in text:
            self.fail("BUS-PROTOCOL.md does not name the shared banned-verb file")
        probes = (
            "test_prb_047a", "test_prb_047", "test_prb_047b",
            "test_prb_108", "test_prb_110", "test_prb_v1",
            "test_prb_r1", "test_prb_r2", "test_prb_r3",
            "test_prb_p1", "test_prb_h1", "test_prb_t1",
            "test_prb_t2", "test_prb_s1", "test_prb_ok",
            "test_prb_c1", "test_prb_g1",
        )
        if len(probes) != 17:
            self.fail("probe list is not 17")
        missing = [name for name in probes if not hasattr(type(self), name)]
        if missing:
            self.fail("missing probes: %s" % missing)
        self.assertEqual(list(REQUIRED), [
            "v", "id", "from", "to", "lane", "priority", "time",
            "routed", "pane", "job", "done-when",
        ])

    def test_prb_t1(self):
        old = _iso(-timedelta(hours=25))
        signed = self._sign(self._job(id="prb-t1", **{"time": old, "routed": "Lead " + old}))
        status, payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED STALE")
        self._assert_not_run()

    def test_prb_t2(self):
        signed = self._sign(self._job(id="prb-t2", **{"time": "3:05 PM ET"}))
        status, payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED MALFORMED_TIME")
        self._assert_not_run()

    def test_prb_s1(self):
        forged = self._job(id="prb-s1")
        status1, _payload1, reply1 = self._process(forged)
        self.assertEqual(status1, 403)
        self.assertEqual(self._assert_verdict_line(reply1), "VERDICT: SPOOFED NO_SIG")
        self._assert_not_run()
        if protocol.find_seen(server.SEEN, "prb-s1"):
            self.fail("spoofed job id was recorded for dedupe")
        rows = self._log_rows()
        if not any(row.get("decision") == "SPOOFED" and row.get("id") == "prb-s1" for row in rows):
            self.fail("spoofed refusal was not logged")
        real = self._sign(self._job(id="prb-s1"))
        status2, _payload2, reply2 = self._process(real)
        self.assertEqual(status2, 200)
        self.assertEqual(self._assert_verdict_line(reply2), "VERDICT: RAN")
        self.assertEqual(self.reached, ["1"])

    def test_prb_ok(self):
        signed = self._sign(self._job(id="prb-ok"))
        status, payload, reply = self._process(signed)
        self.assertEqual(status, 200)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: RAN")
        self.assertEqual(self.reached, ["1"])

    def test_prb_c1(self):
        old = _iso(-timedelta(hours=25))
        first = self._sign(self._job(id="prb-c1", **{"time": old, "routed": "Lead " + old}))
        status, payload, reply = self._process(first)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED STALE")
        self._assert_not_run()
        row = protocol.find_seen(server.SEEN, "prb-c1")
        if not row:
            self.fail("STALE id was not recorded")
        if row.get("content"):
            self.fail("STALE recorded a content hash")
        second = self._sign(self._job(id="prb-c1-r2"))
        status2, _payload2, reply2 = self._process(second)
        self.assertEqual(status2, 200)
        self.assertEqual(self._assert_verdict_line(reply2), "VERDICT: RAN")
        if "prior-id: prb-c1" not in reply2:
            self.fail("re-sign reply did not note the old id")
        self.assertEqual(self.reached, ["1"])

    def test_prb_g1(self):
        """Probe 17: banned verb in job text is BAD_VERB on the hub and the signer."""
        signed = self._sign(self._job(id="prb-g1", job="please send the lab list"))
        status, payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED BAD_VERB")
        self.assertEqual(payload.get("error"), "BAD_VERB")
        self._assert_not_run()

        shell = shutil.which("powershell") or shutil.which("pwsh")
        if not shell:
            self.skipTest("PRB-G1 signer: powershell.exe (5.1) or pwsh not on PATH")
        raw = self.home / "prb-g1-lead.key"
        raw.write_bytes(bytes(self.keys["Lead"]))
        iso = _iso()
        job_file = self.home / "prb-g1.txt"
        job_file.write_text(
            "id: prb-g1s\nv: 2\nfrom: Lead\nto: Build\nlane: lab\npriority: normal\n"
            "time: %s\nrouted: Lead %s\n"
            "job: please send the lab list\ndone-when: the pane lists its files\npane: 1\n"
            % (iso, iso),
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                shell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(ROOT / "tools" / "Sign-BusJob.ps1"),
                "-Owner", "Lead", "-Id", "prb-g1s",
                "-TestKeyPath", str(raw), str(job_file),
            ],
            capture_output=True, text=True, timeout=20,
        )
        if completed.returncode != 3:
            self.fail("PRB-G1 signer did not exit 3: exit %s stdout=%s stderr=%s" % (
                completed.returncode, completed.stdout, completed.stderr,
            ))
        if "BAD_VERB" not in (completed.stdout or ""):
            self.fail("PRB-G1 signer did not print BAD_VERB: %s" % completed.stdout)
        if "SIGNED" in (completed.stdout or ""):
            self.fail("PRB-G1 signer printed SIGNED for a banned verb")

    def test_banned_verbs_file_is_shared(self):
        verbs = protocol.load_banned_verbs()
        self.assertEqual(verbs, ("send", "post", "pay", "delete", "git push", "publish"))
        self.assertEqual(protocol.BANNED_VERBS_PATH, ROOT / "hub" / "banned-verbs.txt")
        script = (ROOT / "tools" / "Sign-BusJob.ps1").read_text(encoding="utf-8")
        if "banned-verbs.txt" not in script:
            self.fail("signer does not load hub/banned-verbs.txt")
        if '"hub"' not in script:
            self.fail("signer does not join the hub folder for banned verbs")
        if "Get-BannedVerbs" not in script:
            self.fail("signer does not load the shared banned-verb file")
        src = Path(server.__file__).read_text(encoding="utf-8")
        if "send|post|pay" in src:
            self.fail("server.py still has its own banned-verb regex")
        self.assertEqual(protocol.verb_reason({"job": "please SEND now"}), "BAD_VERB")
        self.assertEqual(protocol.verb_reason({"job": "list lab pane files"}), "")

    def _run_signer(self, job_id, job_text):
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if not shell:
            return None
        raw = self.home / ("%s-lead.key" % job_id)
        raw.write_bytes(bytes(self.keys["Lead"]))
        job_file = self.home / ("%s.txt" % job_id)
        job_file.write_text(job_text, encoding="utf-8")
        return subprocess.run(
            [
                shell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(ROOT / "tools" / "Sign-BusJob.ps1"),
                "-Owner", "Lead", "-Id", job_id,
                "-TestKeyPath", str(raw), str(job_file),
            ],
            capture_output=True, text=True, timeout=20,
        )

    def _assert_signer_code(self, completed, code, label):
        if completed is None:
            return
        if completed.returncode != 3:
            self.fail("%s did not exit 3: exit %s stdout=%s stderr=%s" % (
                label, completed.returncode, completed.stdout, completed.stderr,
            ))
        if code not in (completed.stdout or ""):
            self.fail("signer did not print %s for %s: %s" % (code, label, completed.stdout))
        if "SIGNED" in (completed.stdout or ""):
            self.fail("signer printed SIGNED for %s" % label)

    def test_verbs_unavailable_missing_or_empty(self):
        path = protocol.BANNED_VERBS_PATH
        original = path.read_bytes()
        iso = _iso()
        clean = (
            "id: verbs-miss-s\nv: 2\nfrom: Lead\nto: Build\nlane: lab\npriority: normal\n"
            "time: %s\nrouted: Lead %s\n"
            "job: list lab pane files\ndone-when: the pane lists its files\npane: 1\n"
            % (iso, iso)
        )
        missing_signer = None
        empty_signer = None
        try:
            path.unlink()
            try:
                protocol.load_banned_verbs()
            except protocol.VerbsUnavailable:
                pass
            except FileNotFoundError:
                self.fail("load_banned_verbs raised FileNotFoundError")
            else:
                self.fail("load_banned_verbs did not refuse a missing file")
            self.assertEqual(protocol.verb_reason({"job": "list lab pane files"}), "VERBS_UNAVAILABLE")
            signed = self._sign(self._job(id="verbs-miss"))
            status, payload, reply = self._process(signed)
            self.assertEqual(status, 403)
            self.assertEqual(payload.get("error"), "VERBS_UNAVAILABLE")
            self.assertNotEqual(payload.get("error"), "RETURN")
            self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED VERBS_UNAVAILABLE")
            self._assert_not_run()
            missing_signer = self._run_signer("verbs-miss-s", clean)

            path.write_text("# only comments\n\n", encoding="utf-8")
            self.assertEqual(protocol.verb_reason({"job": "list lab pane files"}), "VERBS_UNAVAILABLE")
            signed2 = self._sign(self._job(id="verbs-empty"))
            status2, payload2, reply2 = self._process(signed2)
            self.assertEqual(status2, 403)
            self.assertEqual(payload2.get("error"), "VERBS_UNAVAILABLE")
            self.assertNotEqual(payload2.get("error"), "RETURN")
            self.assertEqual(self._assert_verdict_line(reply2), "VERDICT: REFUSED VERBS_UNAVAILABLE")
            empty_job = clean.replace("verbs-miss-s", "verbs-empty-s")
            empty_signer = self._run_signer("verbs-empty-s", empty_job)
        finally:
            path.write_bytes(original)
        self._assert_signer_code(missing_signer, "VERBS_UNAVAILABLE", "missing banned-verbs.txt")
        self._assert_signer_code(empty_signer, "VERBS_UNAVAILABLE", "empty banned-verbs.txt")

    def test_banned_verbs_file_edit_applies_to_both_sides(self):
        path = protocol.BANNED_VERBS_PATH
        original = path.read_bytes()
        iso = _iso()
        body = (
            "id: verbs-edit-s\nv: 2\nfrom: Lead\nto: Build\nlane: lab\npriority: normal\n"
            "time: %s\nrouted: Lead %s\n"
            "job: please exfiltrate the lab list\ndone-when: the pane lists its files\npane: 1\n"
            % (iso, iso)
        )
        completed = None
        try:
            path.write_text(path.read_text(encoding="utf-8") + "exfiltrate\n", encoding="utf-8")
            self.assertEqual(protocol.verb_reason({"job": "please exfiltrate the list"}), "BAD_VERB")
            signed = self._sign(self._job(id="verbs-edit", job="please exfiltrate the lab list"))
            status, payload, reply = self._process(signed)
            self.assertEqual(status, 403)
            self.assertEqual(payload.get("error"), "BAD_VERB")
            self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED BAD_VERB")
            self._assert_not_run()
            completed = self._run_signer("verbs-edit-s", body)
        finally:
            path.write_bytes(original)
        self.assertEqual(protocol.verb_reason({"job": "please exfiltrate the list"}), "")
        self._assert_signer_code(completed, "BAD_VERB", "edited banned-verbs.txt")

    def test_verb_match_normalizes_long_s(self):
        self.assertEqual(protocol.normalize_verb_text("ſend"), "send")
        self.assertEqual(protocol.verb_reason({"job": "please ſend the lab list"}), "BAD_VERB")
        self.assertEqual(protocol.verb_reason({"job": "please se\u200bnd now"}), "BAD_VERB")
        self.assertEqual(protocol.verb_reason({"job": "please  git   push  now"}), "BAD_VERB")
        iso = _iso()
        body = (
            "id: verbs-longs\nv: 2\nfrom: Lead\nto: Build\nlane: lab\npriority: normal\n"
            "time: %s\nrouted: Lead %s\n"
            "job: please ſend the lab list\ndone-when: the pane lists its files\npane: 1\n"
            % (iso, iso)
        )
        completed = self._run_signer("verbs-longs", body)
        self._assert_signer_code(completed, "BAD_VERB", "ſend")

    def test_hub_owner_mismatch_exact_from(self):
        signed = self._sign(self._job(id="own-exact", **{"from": "GrokBot"}), owner="Grok Bot")
        status, payload, reply = self._process(signed, "GROKBOT-TO-BUILD.md")
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "SPOOFED OWNER_MISMATCH")
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: SPOOFED OWNER_MISMATCH")
        self._assert_not_run()

    def test_p4c_hub_field_codes(self):
        signed = self._sign(self._job(id="p4c-miss", **{"done-when": ""}))
        status, payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED MISSING_FIELD")
        self.assertEqual(payload.get("error"), "MISSING_FIELD")
        self._assert_not_run()

        iso = _iso()
        body = (
            "id: p4c-span\nv: 2\nfrom: Lead\nto: Build\nlane: lab\n"
            "priority: normal\n extra\n"
            "time: " + iso + "\nrouted: Lead " + iso + "\npane: 1\n"
            "job: list lab pane files\ndone-when: the pane lists its files\n"
        )
        text = self._sign_raw_block(body)
        status2, payload2, reply2 = self._process_raw(text, "p4c-span")
        self.assertEqual(status2, 403)
        self.assertEqual(self._assert_verdict_line(reply2), "VERDICT: REFUSED BAD_FIELD")
        self.assertEqual(payload2.get("error"), "BAD_FIELD")
        self._assert_not_run()

        signed3 = self._sign(self._job(id="p4c-pane", pane="01"))
        status3, payload3, reply3 = self._process(signed3)
        self.assertEqual(status3, 403)
        self.assertEqual(self._assert_verdict_line(reply3), "VERDICT: REFUSED BAD_PANE")
        self._assert_not_run()

        signed4 = self._sign(self._job(id="p4c-from", **{"from": "GrokBot"}), owner="Lead")
        self._write(signed4)
        status4, payload4 = server.apply_decision("p4c-from", "approve")
        reply4 = self._last_reply("Lead")
        self.assertEqual(status4, 403)
        self.assertIn("OWNER_MISMATCH", payload4.get("error", ""))
        self._assert_not_run()
        self._assert_clean(reply4)

    def test_prb_c2(self):
        old = _iso(-timedelta(hours=25))
        first = self._sign(self._job(id="prb-c2", **{"time": old, "routed": "Lead " + old}))
        status, payload, reply = self._process(first)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED STALE")
        reused = self._sign(self._job(id="prb-c2"))
        status2, _payload2, reply2 = self._process(reused)
        self.assertEqual(status2, 403)
        self.assertEqual(self._assert_verdict_line(reply2), "VERDICT: DUPLICATE")
        self._assert_not_run()

    def test_prb_m2_restamped_routed(self):
        first = self._sign(self._job(id="prb-m2a"))
        status1, _payload1, reply1 = self._process(first)
        self.assertEqual(status1, 200)
        later = _iso(timedelta(minutes=1))
        earlier = _iso(-timedelta(minutes=1))
        second = self._sign(self._job(
            id="prb-m2b",
            **{"time": later},
            routed="Grok Bot " + earlier,
        ))
        status2, _payload2, reply2 = self._process(second)
        self.assertEqual(status2, 403)
        self.assertEqual(self._assert_verdict_line(reply2), "VERDICT: DUPLICATE SAME_CONTENT")
        self.assertEqual(self.reached, ["1"])

    def test_future_time_bad_time(self):
        future = _iso(timedelta(minutes=10))
        signed = self._sign(self._job(id="prb-future", **{"time": future, "routed": "Lead " + future}))
        status, payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED BAD_TIME")
        self._assert_not_run()

    def test_iso_offset_roundtrip(self):
        parsed = protocol.parse_iso_time("2026-10-10T15:05:00-04:00")
        if parsed is None:
            self.fail("ISO offset time did not parse")
        if parsed.utcoffset() != timedelta(hours=-4):
            self.fail("ISO offset was not kept")
        if protocol.parse_iso_time("2026-10-10T15:05:00") is not None:
            self.fail("missing offset was accepted")
        if protocol.parse_iso_time("3:05 PM ET") is not None:
            self.fail("AM/PM ET was accepted")

    def test_time_regex_long_whitespace(self):
        blob = " " * 100000
        start = time.perf_counter()
        got = protocol.parse_iso_time(blob)
        elapsed_ms = (time.perf_counter() - start) * 1000
        if got is not None:
            self.fail("overlong whitespace parsed as a time")
        if elapsed_ms >= 100:
            self.fail("time parse took too long")

    def test_seen_survives_reread(self):
        signed = self._sign(self._job(id="prb-seen"))
        self._process(signed)
        rows = protocol.read_seen(server.SEEN)
        if not any(row.get("id") == "prb-seen" and row.get("content") and row.get("at") for row in rows):
            self.fail("seen-list did not keep id, content hash, and expiry time")
        for row in rows:
            if "verdict" in row or "job" in row or "reason" in row:
                self.fail("seen-list stored a field beyond id, content, and at")
        again = protocol.read_seen(server.SEEN)
        self.assertEqual(len(again), len(rows))

    def test_seen_expires_resent_is_stale(self):
        old = _iso(-timedelta(hours=25))
        signed = self._sign(self._job(id="prb-old", **{"time": old, "routed": "Lead " + old}))
        digest = protocol.content_fingerprint(signed)
        aged = (datetime.now(timezone.utc) - timedelta(days=protocol.SEEN_RETENTION_DAYS + 1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        server.SEEN.write_text(
            json.dumps({"id": "prb-old", "content": digest, "at": aged}, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        status, payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED STALE")
        self._assert_not_run()
        if not any(row.get("decision") == "prune" for row in self._log_rows()):
            self.fail("seen-list prune was not logged")

    def test_seen_migrate_plain_ids(self):
        server.SEEN.write_text("keep-a\nkeep-b\n", encoding="utf-8")
        rows = protocol.read_seen(server.SEEN)
        ids = {row.get("id") for row in rows}
        if ids != {"keep-a", "keep-b"}:
            self.fail("plain-id migration lost an id")
        text = server.SEEN.read_text(encoding="utf-8")
        if "keep-a" not in text or "keep-b" not in text:
            self.fail("migrated file dropped an id")
        if not text.lstrip().startswith("{"):
            self.fail("plain ids were not rewritten to the new format")

    def test_seen_corrupt_fail_closed(self):
        original = '{"id":""}\n'
        server.SEEN.write_text(original, encoding="utf-8")
        signed = self._sign(self._job(id="prb-corrupt"))
        status, payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED SEEN_LIST")
        self._assert_not_run()
        if server.SEEN.read_text(encoding="utf-8") != original:
            self.fail("corrupt seen-list was rewritten")
        if not any(row.get("decision") == "alarm" and row.get("reason") == "SEEN_LIST" for row in self._log_rows()):
            self.fail("corrupt seen-list did not raise an alarm")

    def test_seen_plain_id_starting_with_brace_migrates(self):
        server.SEEN.write_text("{legacy-id\nkeep-b\n", encoding="utf-8")
        rows = protocol.read_seen(server.SEEN)
        ids = {row.get("id") for row in rows}
        if ids != {"{legacy-id", "keep-b"}:
            self.fail("plain id starting with '{' did not migrate")
        text = server.SEEN.read_text(encoding="utf-8")
        if "{legacy-id" not in text or "keep-b" not in text:
            self.fail("migrated file dropped a brace-prefixed id")
        if not any(row.get("decision") == "migrate" for row in self._log_rows()):
            # migrate is logged via take_seen_events only when server.seen_ids runs
            pass
        parsed = [json.loads(line) for line in text.splitlines() if line.strip()]
        if any(row.get("id") == "{legacy-id" and not row.get("at") for row in parsed):
            self.fail("brace-prefixed plain id was not given a timestamp")

    def test_10_no_secrets_in_protocol_outputs(self):
        signed = self._sign(self._job(id="prb-scan"))
        self._process(signed)
        self._assert_clean(self._return_text("Lead"), _OUTPUT.getvalue())
        for row in protocol.read_seen(server.SEEN):
            for key in row:
                if key not in protocol.SEEN_KEYS:
                    self.fail("seen-list has an extra field")

    def test_seen_retention_at_least_30_days(self):
        if protocol.SEEN_RETENTION_DAYS < 30:
            self.fail("SEEN_RETENTION_DAYS is below 30")

    def test_seen_keeps_29_day_row(self):
        clock = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
        aged = (clock - timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%SZ")
        server.SEEN.write_text(
            json.dumps({"id": "keep-29", "content": "abc", "at": aged}, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        rows = protocol.read_seen(server.SEEN, now=clock)
        if not any(row.get("id") == "keep-29" for row in rows):
            self.fail("29-day-old seen row was dropped")

    def test_job_age_24h_boundary(self):
        clock = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
        exact = (clock - timedelta(hours=24)).astimezone(ET).replace(microsecond=0).isoformat()
        over = (clock - timedelta(hours=24, seconds=1)).astimezone(ET).replace(microsecond=0).isoformat()
        ok = self._job(id="bound-24", **{"time": exact, "routed": "Lead " + exact})
        stale = self._job(id="bound-24-over", **{"time": over, "routed": "Lead " + over})
        if protocol.job_time_reason(ok, now=clock) != "":
            self.fail("exactly 24 hours old was not accepted")
        if protocol.job_time_reason(stale, now=clock) != "STALE":
            self.fail("just over 24 hours old was not STALE")

    def test_job_age_5min_boundary(self):
        clock = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)
        exact = (clock + timedelta(minutes=5)).astimezone(ET).replace(microsecond=0).isoformat()
        over = (clock + timedelta(minutes=5, seconds=1)).astimezone(ET).replace(microsecond=0).isoformat()
        ok = self._job(id="bound-5", **{"time": exact, "routed": "Lead " + exact})
        future = self._job(id="bound-5-over", **{"time": over, "routed": "Lead " + over})
        if protocol.job_time_reason(ok, now=clock) != "":
            self.fail("exactly 5 minutes ahead was not accepted")
        if protocol.job_time_reason(future, now=clock) != "BAD_TIME":
            self.fail("just over 5 minutes ahead was not BAD_TIME")

    def test_priority_before_time_probe(self):
        old = _iso(-timedelta(hours=25))
        signed = self._sign(self._job(
            id="pri-before-time",
            priority="high",
            **{"time": old, "routed": "Lead " + old},
        ))
        status, payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED BAD_PRIORITY")
        self.assertEqual(payload.get("error"), "BAD_PRIORITY")
        self._assert_not_run()

    def test_b1_malformed_time_month_13(self):
        signed = self._sign(self._job(id="b1-m13", **{"time": "2026-13-10T15:05:00-04:00"}))
        status, payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertNotEqual(status, 500)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED MALFORMED_TIME")
        self.assertEqual(payload.get("error"), "MALFORMED_TIME")
        self._assert_not_run()

    def test_b1_malformed_time_offset_minus_24(self):
        signed = self._sign(self._job(id="b1-off24", **{"time": "2026-10-10T15:05:00-24:00"}))
        status, payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertNotEqual(status, 500)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED MALFORMED_TIME")
        self.assertEqual(payload.get("error"), "MALFORMED_TIME")
        self._assert_not_run()

    def test_b1_malformed_time_arabic_digits(self):
        signed = self._sign(self._job(id="b1-arabic", **{"time": "٢٠٢٦-١٠-١٠T١٥:٠٥:٠٠-٠٤:٠٠"}))
        status, payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertNotEqual(status, 500)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED MALFORMED_TIME")
        self.assertEqual(payload.get("error"), "MALFORMED_TIME")
        self._assert_not_run()

    def test_parse_iso_time_never_raises(self):
        for value in (
            "2026-13-10T15:05:00-04:00",
            "2026-10-10T15:05:00-24:00",
            "٢٠٢٦-١٠-١٠T١٥:٠٥:٠٠-٠٤:٠٠",
            "2026-10-10T15:05:00Z",
            "2026-10-10T15:05:00+00:00",
        ):
            try:
                got = protocol.parse_iso_time(value)
            except Exception:
                self.fail("parse_iso_time raised on %s" % value)
            if got is not None:
                self.fail("parse_iso_time accepted %s" % value)

    def test_zulu_and_non_et_offsets_malformed(self):
        cases = (
            ("zulu-1", "2026-10-10T15:05:00Z"),
            ("utc-1", "2026-10-10T15:05:00+00:00"),
            ("plus1-1", "2026-10-10T20:05:00+01:00"),
        )
        for job_id, value in cases:
            signed = self._sign(self._job(id=job_id, **{"time": value}))
            status, payload, reply = self._process(signed)
            self.assertEqual(status, 403, job_id)
            self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED MALFORMED_TIME", job_id)
            self._assert_not_run()

    def test_queue_and_format_reply_no_500(self):
        signed = self._sign(self._job(id="no-500", **{"time": "2026-13-01T00:00:00-04:00"}))
        self._write(signed)
        try:
            items = server.queue()
        except Exception:
            self.fail("queue() raised on an impossible date")
        if not items:
            self.fail("queue() dropped the job")
        self.assertEqual(items[0].get("reason"), "MALFORMED_TIME")
        try:
            text = protocol.format_reply(
                protocol.Verdict("REFUSED", "MALFORMED_TIME"),
                signed,
            )
        except Exception:
            self.fail("format_reply raised")
        self.assertEqual(text.splitlines()[0], "VERDICT: REFUSED MALFORMED_TIME")

    def test_denied_is_recorded(self):
        signed = self._sign(self._job(id="deny-1"))
        self._write(signed)
        status, payload = server.apply_decision("deny-1", "deny")
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("result"), "denied")
        reply = self._last_reply()
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: DENIED")
        row = protocol.find_seen(server.SEEN, "deny-1")
        if not row:
            self.fail("DENIED job was not recorded")
        if not row.get("content"):
            self.fail("DENIED did not record a content hash")
        status2, _payload2, reply2 = self._process(signed)
        self.assertEqual(status2, 403)
        self.assertEqual(self._assert_verdict_line(reply2), "VERDICT: DUPLICATE")

    def test_seen_unicode_decode_error_alarms(self):
        server.SEEN.write_bytes(b"\xff\xfe not-utf8")
        signed = self._sign(self._job(id="bad-utf8"))
        status, payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED SEEN_LIST")
        self._assert_not_run()
        if not any(row.get("decision") == "alarm" and row.get("reason") == "SEEN_LIST" for row in self._log_rows()):
            self.fail("UnicodeDecodeError did not raise an alarm")

    def test_seen_first_run_vs_deleted_file(self):
        if server.SEEN.is_file() or protocol.seen_marker_path(server.SEEN).is_file():
            self.fail("first run started with a seen file or marker")
        first = self._sign(self._job(id="first-run"))
        status, _payload, reply = self._process(first)
        self.assertEqual(status, 200)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: RAN")
        if any(row.get("decision") == "alarm" for row in self._log_rows()):
            self.fail("first run raised a seen-list alarm")
        if not protocol.seen_marker_path(server.SEEN).is_file():
            self.fail("first write did not create the init marker")
        server.SEEN.unlink()
        second = self._sign(self._job(id="after-delete"))
        status2, payload2, reply2 = self._process(second)
        self.assertEqual(status2, 403)
        self.assertEqual(self._assert_verdict_line(reply2), "VERDICT: REFUSED SEEN_LIST")
        self.assertEqual(self.reached, ["1"])

    def test_seen_alarm_once_per_state_change(self):
        signed = self._sign(self._job(id="alarm-poll"))
        self._write(signed)
        server.SEEN.write_text('{"id":""}\n', encoding="utf-8")
        server.queue()
        server.queue()
        alarms = [row for row in self._log_rows() if row.get("decision") == "alarm"]
        if len(alarms) != 1:
            self.fail("seen-list alarm was not once per state change")

    def test_seen_dump_is_atomic(self):
        src = (ROOT / "hub" / "protocol.py").read_text(encoding="utf-8")
        if "os.replace" not in src:
            self.fail("seen-list write is not atomic")
        original = json.dumps(
            {"id": "keep-orig", "content": "abc", "at": "2026-10-10T15:00:00Z"},
            separators=(",", ":"),
        ) + "\n"
        server.SEEN.write_text(original, encoding="utf-8")

        def boom(src_path, dst_path):
            raise OSError("locked")

        with mock.patch("hub.protocol.os.replace", boom):
            try:
                protocol.record_seen(
                    server.SEEN,
                    self._job(id="atomic-1"),
                    protocol.Verdict("RAN", "", http="RAN"),
                )
            except protocol.SeenListError:
                pass
            else:
                self.fail("failed replace did not raise SeenListError")
        if server.SEEN.read_text(encoding="utf-8") != original:
            self.fail("original seen-list was changed after a failed replace")
        leftovers = [
            path for path in server.SEEN.parent.iterdir()
            if path.name.startswith(server.SEEN.name + ".") and path.suffix == ".tmp"
        ]
        if leftovers:
            self.fail("partial temp file was left behind")

    def test_seen_migrate_log_row(self):
        server.SEEN.write_text("keep-a\nkeep-b\n", encoding="utf-8")
        signed = self._sign(self._job(id="after-mig"))
        self._process(signed)
        if not any(row.get("decision") == "migrate" for row in self._log_rows()):
            self.fail("plain-id migrate was not logged")

    def test_seen_brace_mixed_with_json_alarms(self):
        original = (
            json.dumps({"id": "keep-json", "content": "x", "at": "2026-10-10T15:00:00Z"}, separators=(",", ":"))
            + "\n{legacy-id\n"
        )
        server.SEEN.write_text(original, encoding="utf-8")
        try:
            protocol.read_seen(server.SEEN)
        except protocol.SeenListError:
            pass
        else:
            self.fail("mixed JSON and legacy '{' line did not alarm")
        if server.SEEN.read_text(encoding="utf-8") != original:
            self.fail("mixed seen-list was rewritten")

    def test_seen_bom_stripped_from_legacy_id(self):
        server.SEEN.write_bytes(b"\xef\xbb\xbf{legacy-id\nkeep-b\n")
        rows = protocol.read_seen(server.SEEN)
        ids = {row.get("id") for row in rows}
        if ids != {"{legacy-id", "keep-b"}:
            self.fail("BOM was not stripped from the first legacy id")

    def test_seen_init_marker_on_first_read(self):
        if server.SEEN.is_file() or protocol.seen_marker_path(server.SEEN).is_file():
            self.fail("first read started with a seen file or marker")
        protocol.read_seen(server.SEEN)
        if not protocol.seen_marker_path(server.SEEN).is_file():
            self.fail("first read did not write the init marker")
        if not server.SEEN.is_file():
            self.fail("first read did not create the seen file")

    def test_seen_missing_with_approvals_history_alarms(self):
        server.LOG.write_text(
            json.dumps({"time": "now", "id": "old", "decision": "approve"}) + "\n",
            encoding="utf-8",
        )
        protocol.set_approvals_log(server.LOG)
        first = self._sign(self._job(id="hist-1"))
        status, payload, reply = self._process(first)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED SEEN_LIST")
        self._assert_not_run()
        if server.SEEN.is_file() or protocol.seen_marker_path(server.SEEN).is_file():
            self.fail("history alarm recreated the seen file or marker")
        old = self._sign(self._job(id="hist-old"))
        status2, _payload2, reply2 = self._process(old)
        self.assertEqual(status2, 403)
        self.assertEqual(self._assert_verdict_line(reply2), "VERDICT: REFUSED SEEN_LIST")
        self._assert_not_run()

    def test_marker_only_alarms(self):
        protocol.seen_marker_path(server.SEEN).write_text("1\n", encoding="utf-8")
        signed = self._sign(self._job(id="marker-only"))
        status, payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED SEEN_LIST")
        self._assert_not_run()
        if server.SEEN.is_file():
            self.fail("marker-only alarm created a seen file")

    def test_stopped_is_recorded(self):
        body = (
            "id: rec-stop\n" + self._header()
            + "job: list lab pane files\n"
            + protocol.STOP_TOKEN
            + "\ndone-when: the pane lists its files\n"
        )
        text = self._sign_raw_block(body)
        status, payload, reply = self._process_raw(text, "rec-stop")
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: STOPPED")
        row = protocol.find_seen(server.SEEN, "rec-stop")
        if not row:
            self.fail("STOPPED id was not recorded")
        if not row.get("content"):
            self.fail("STOPPED did not record a content hash")

    def test_duplicate_is_recorded(self):
        first = self._sign(self._job(id="rec-dup-a"))
        self._process(first)
        second = self._sign(self._job(id="rec-dup-b"))
        status, payload, reply = self._process(second)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: DUPLICATE SAME_CONTENT")
        row = protocol.find_seen(server.SEEN, "rec-dup-b")
        if not row:
            self.fail("DUPLICATE id was not recorded")
        if row.get("content"):
            self.fail("DUPLICATE recorded a content hash")

    def test_claim_before_run_and_crash_before_record(self):
        signed = self._sign(self._job(id="claim-crash"))
        protocol.read_seen(server.SEEN)
        order = []
        real_claim = protocol.claim_seen

        def claim_wrap(path, fields, **kwargs):
            order.append("claim")
            real_claim(path, fields, **kwargs)
            row = protocol.find_seen(path, fields.get("id"))
            if not row or not protocol.is_claim(row):
                raise AssertionError("claim was not on disk before run")

        def runner(pane, command):
            order.append("run")
            self.reached.append(pane)
            return 0, "pane-ok", ""

        server.set_run_impl(runner)
        self._write(signed)
        with mock.patch("hub.protocol.claim_seen", claim_wrap):
            with mock.patch("hub.protocol.finalize_seen", side_effect=RuntimeError("crash")):
                try:
                    server.apply_decision("claim-crash", "approve")
                except RuntimeError:
                    pass
        if order != ["claim", "run"]:
            self.fail("claim logic did not run before run_pane: %s" % order)
        row = protocol.find_seen(server.SEEN, "claim-crash")
        if not row or not protocol.is_claim(row):
            self.fail("crash before record lost the on-disk claim")
        server._seen_alarm_state = None
        protocol.take_seen_events()
        status, payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: DUPLICATE")
        self.assertEqual(self.reached, ["1"])
        if not any(row.get("decision") == "alarm" for row in self._log_rows()):
            self.fail("unfinished claim did not raise an alarm")

    def test_claim_write_all_retries_fail_does_not_run(self):
        protocol.read_seen(server.SEEN)
        signed = self._sign(self._job(id="claim-fail"))
        self._write(signed)

        def boom(src_path, dst_path):
            raise OSError("locked")

        with mock.patch("hub.protocol.os.replace", boom):
            status, payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED SEEN_LIST")
        self._assert_not_run()

    def test_finalize_all_retries_fail_record_failed(self):
        protocol.read_seen(server.SEEN)
        signed = self._sign(self._job(id="fin-fail"))
        real = os.replace
        state = {"n": 0}

        def flaky(src_path, dst_path):
            state["n"] += 1
            if state["n"] == 1:
                return real(src_path, dst_path)
            raise OSError("locked")

        with mock.patch("hub.protocol.os.replace", flaky):
            status, payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "RAN RECORD_FAILED")
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: RAN")
        if "RECORD: FAILED" not in reply:
            self.fail("return file was a bare RAN after a failed record")
        self.assertEqual(self.reached, ["1"])
        row = protocol.find_seen(server.SEEN, "fin-fail")
        if not row or not protocol.is_claim(row):
            self.fail("failed finalize did not leave the claim")
        server._seen_alarm_state = None
        protocol.take_seen_events()
        status2, _payload2, reply2 = self._process(signed)
        self.assertEqual(status2, 403)
        self.assertEqual(self._assert_verdict_line(reply2), "VERDICT: DUPLICATE")
        self.assertEqual(self.reached, ["1"])

    def test_ran_write_fail_once_same_job_cannot_run_twice(self):
        real = os.replace
        state = {"n": 0}

        def flaky(src_path, dst_path):
            state["n"] += 1
            if state["n"] == 1:
                raise OSError("locked")
            return real(src_path, dst_path)

        signed = self._sign(self._job(id="ran-once"))
        with mock.patch("hub.protocol.os.replace", flaky):
            status, payload, reply = self._process(signed)
        self.assertEqual(status, 200)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: RAN")
        self.assertEqual(self.reached, ["1"])
        status2, _payload2, reply2 = self._process(signed)
        self.assertEqual(status2, 403)
        self.assertEqual(self._assert_verdict_line(reply2), "VERDICT: DUPLICATE")
        self.assertEqual(self.reached, ["1"])

    def test_signing_md_signer_fills_time_and_routed(self):
        text = (ROOT / "docs" / "SIGNING.md").read_text(encoding="utf-8")
        if "fills in `time`" not in text:
            self.fail("SIGNING.md does not say the signer fills in time")
        if "must already be exactly" not in text:
            self.fail("SIGNING.md does not require the full routed form")
        if "There is no GB alias" not in text:
            self.fail("SIGNING.md does not refuse a GB alias")
        if "match `-Owner`" not in text:
            self.fail("SIGNING.md does not say the signer requires router to match -Owner")

    def test_upgrade_from_main_writes_init_and_does_not_lock(self):
        server.LOG.write_text(
            json.dumps({"time": "now", "id": "pre-upgrade", "decision": "approve"}) + "\n",
            encoding="utf-8",
        )
        protocol.set_approvals_log(server.LOG)
        server.SEEN.write_text("pre-upgrade\nplain-from-main\n", encoding="utf-8")
        marker = protocol.seen_marker_path(server.SEEN)
        if marker.is_file():
            marker.unlink()
        signed = self._sign(self._job(id="after-upgrade"))
        status, payload, reply = self._process(signed)
        self.assertEqual(status, 200)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: RAN")
        self.assertEqual(self.reached, ["1"])
        if not marker.is_file():
            self.fail("upgrade from main did not write .init")
        rows = protocol.read_seen(server.SEEN)
        ids = {row.get("id") for row in rows}
        if "pre-upgrade" not in ids or "plain-from-main" not in ids:
            self.fail("upgrade from main dropped a plain seen id")
        if any(row.get("decision") == "alarm" and row.get("reason") == "SEEN_LIST" for row in self._log_rows()):
            self.fail("upgrade from main locked the hub")

    def test_upgrade_alarms_if_recent_approved_id_missing_from_seen(self):
        iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        server.LOG.write_text(
            json.dumps({"time": iso, "id": "missing-recent", "decision": "approve"}) + "\n",
            encoding="utf-8",
        )
        protocol.set_approvals_log(server.LOG)
        server.SEEN.write_text("other-id\n", encoding="utf-8")
        marker = protocol.seen_marker_path(server.SEEN)
        if marker.is_file():
            marker.unlink()
        signed = self._sign(self._job(id="after-gap"))
        status, payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED SEEN_LIST")
        self._assert_not_run()
        if marker.is_file():
            self.fail("gap alarm wrote .init")

    def test_unreadable_log_time_counts_as_recent(self):
        protocol.set_approvals_log(server.LOG)
        server.LOG.write_text(
            json.dumps({"time": "not-a-time", "id": "unread-recent", "decision": "approve"}) + "\n",
            encoding="utf-8",
        )
        found = protocol.recent_approved_or_run_ids()
        if "unread-recent" not in found:
            self.fail("unreadable approval time did not count as recent")
        server.SEEN.write_text("other-id\n", encoding="utf-8")
        marker = protocol.seen_marker_path(server.SEEN)
        if marker.is_file():
            marker.unlink()
        signed = self._sign(self._job(id="after-unread"))
        status, _payload, reply = self._process(signed)
        self.assertEqual(status, 403)
        self.assertEqual(self._assert_verdict_line(reply), "VERDICT: REFUSED SEEN_LIST")
        self._assert_not_run()

    def test_et_naive_21h_and_23h_stay_in_gap_set(self):
        clock = datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc)
        local = clock.astimezone(ET)
        rows = []
        for hours, job_id in ((21, "et-21h"), (23, "et-23h")):
            naive = (local - timedelta(hours=hours)).replace(tzinfo=None)
            rows.append(json.dumps({
                "time": naive.strftime("%Y-%m-%dT%H:%M:%S"),
                "id": job_id,
                "decision": "approve",
            }))
        server.LOG.write_text("\n".join(rows) + "\n", encoding="utf-8")
        protocol.set_approvals_log(server.LOG)
        found = protocol.recent_approved_or_run_ids(now=clock)
        if "et-21h" not in found or "et-23h" not in found:
            self.fail("ET 21h/23h naive rows dropped from the gap set: %s" % found)
        src = inspect.getsource(protocol._parse_log_time)
        naive_branch = src.split("if parsed.tzinfo is None:")[1].split("return parsed")[0]
        if "timezone.utc" in naive_branch:
            self.fail("revert-to-UTC mutant: naive fromisoformat uses UTC")
        if "fold=1" not in inspect.getsource(protocol._naive_as_et) and "fold=1" not in src:
            self.fail("naive ET attach is missing fold=1")

    def test_nov1_repeated_hour_keeps_fold1(self):
        naive = "2026-11-01T01:30:00"
        parsed = protocol._parse_log_time(naive)
        if parsed is None:
            self.fail("Nov 1 01:30 naive stamp did not parse")
        fold0 = datetime(2026, 11, 1, 1, 30).replace(tzinfo=ET, fold=0)
        fold1 = datetime(2026, 11, 1, 1, 30).replace(tzinfo=ET, fold=1)
        if fold0.utcoffset() == fold1.utcoffset():
            self.fail("Nov 1 01:30 is not a repeated hour")
        if parsed.fold != 1 or parsed.utcoffset() != fold1.utcoffset():
            self.fail("fold=1 mutant: Nov 1 01:30 used fold=%s offset=%s" % (
                parsed.fold, parsed.utcoffset(),
            ))
        clock = fold1.astimezone(timezone.utc) + timedelta(hours=23, minutes=45)
        if clock - fold0.astimezone(timezone.utc) <= timedelta(hours=24):
            self.fail("test clock does not distinguish fold=0 from fold=1")
        server.LOG.write_text(
            json.dumps({"time": naive, "id": "dst-fold", "decision": "approve"}) + "\n",
            encoding="utf-8",
        )
        protocol.set_approvals_log(server.LOG)
        found = protocol.recent_approved_or_run_ids(now=clock)
        if "dst-fold" not in found:
            self.fail("fold=1 mutant: Nov 1 01:30 dropped from the gap set")
        if "fold=1" not in inspect.getsource(protocol._naive_as_et):
            self.fail("fold=1 mutant: _naive_as_et dropped fold=1")

    def test_now_writes_utc_z_not_naive_local(self):
        stamp = server.now()
        if not stamp.endswith("Z"):
            self.fail("naive now() mutant: stamp is not UTC Z: %s" % stamp)
        parsed = datetime.fromisoformat(stamp[:-1] + "+00:00")
        if parsed.tzinfo is None:
            self.fail("naive now() mutant: stamp has no offset")
        src = inspect.getsource(server.now)
        if "timezone.utc" not in src:
            self.fail("naive now() mutant: now() does not use timezone.utc")
        if "%Y-%m-%d %H:%M:%S" in src:
            self.fail("naive now() mutant: now() still writes local wall time")

    def test_seen_claim_alarm_once_then_reset_after_clean_read(self):
        protocol.read_seen(server.SEEN)
        signed = self._sign(self._job(id="claim-alarm"))
        protocol.claim_seen(server.SEEN, signed)
        server.seen_ids()
        server.seen_ids()
        claims = [row for row in self._log_rows() if row.get("reason") == "SEEN_CLAIM"]
        if len(claims) != 1:
            self.fail("SEEN_CLAIM was not once per state change: %s" % len(claims))
        protocol.finalize_seen(server.SEEN, signed, protocol.Verdict("RAN", "", http="RAN"))
        server.seen_ids()
        if server._seen_alarm_state != "ok":
            self.fail("clean read with no leftover claims did not reset alarm state")
        if server._seen_claim_alarmed:
            self.fail("clean read did not clear the claim-alarm latch")
        server.SEEN.write_text('{"id":""}\n', encoding="utf-8")
        try:
            server.seen_ids()
        except protocol.SeenListError:
            pass
        else:
            self.fail("corrupt seen-list did not fail closed")
        later = [row for row in self._log_rows() if row.get("reason") == "SEEN_LIST"]
        if not later:
            self.fail("later seen-list failure did not alarm after a clean reset")

    def test_pane_mismatch_after_claim_drops_claim(self):
        protocol.read_seen(server.SEEN)
        signed = self._sign(self._job(id="pane-miss", pane="1"))
        protocol.claim_seen(server.SEEN, signed)
        row = protocol.find_seen(server.SEEN, "pane-miss")
        if not row or not protocol.is_claim(row):
            self.fail("claim was not on disk before PANE_MISMATCH")
        code, _out, err = server.run_pane("2", signed["job"], signed, "Lead")
        self.assertEqual(code, 403)
        self.assertEqual(err, "PANE_MISMATCH")
        leftover = protocol.find_seen(server.SEEN, "pane-miss")
        if leftover:
            self.fail("PANE_MISMATCH left the id on the seen-list")
        self._assert_not_run()

    def test_claim_seen_rejects_id_already_present(self):
        protocol.read_seen(server.SEEN)
        signed = self._sign(self._job(id="claim-twice"))
        protocol.claim_seen(server.SEEN, signed)
        try:
            protocol.claim_seen(server.SEEN, signed)
        except protocol.SeenListError:
            pass
        else:
            self.fail("claim_seen on an id already present did not fail")
        row = protocol.find_seen(server.SEEN, "claim-twice")
        if not row or not protocol.is_claim(row):
            self.fail("first claim was lost after a second claim_seen")


if __name__ == "__main__":
    unittest.main(verbosity=2)
