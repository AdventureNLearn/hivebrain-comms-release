"""SPEC-HBC-SIG-LOCK-1010 tests 1-14. Temporary test keys only."""

import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hub import protocol, sigverify
import server
from tests.live_guard import install_live_state_guard

install_live_state_guard()

SIGNED_FIELDS = sigverify.SIGNED_FIELDS
RULE13 = "rule 13 in the user's bus protocol file"


def _shift_iso(days):
    when = datetime.now(timezone.utc) + timedelta(days=days)
    return when.strftime("%Y-%m-%dT%H:%M:%S+00:00")

V1_FIELDS = ("id", "from", "to", "lane", "priority", "time", "routed", "job", "done-when")
WRITE_ORDER = ("id", "v", "from", "to", "lane", "priority", "time", "routed", "pane", "job", "done-when", "sig")

GOLDEN = (
    {
        "v": "2",
        "id": "vec-1",
        "from": "Lead",
        "to": "Build",
        "lane": "lab",
        "priority": "normal",
        "time": "2026-10-10 12:00",
        "routed": "2026-10-10 12:01",
        "pane": "1",
        "job": "list lab pane files",
        "done-when": "the pane lists its files",
        "canonical": (
            "v=2\nid=vec-1\nfrom=Lead\nto=Build\nlane=lab\npriority=normal\n"
            "time=2026-10-10 12:00\nrouted=2026-10-10 12:01\npane=1\n"
            "job=list lab pane files\ndone-when=the pane lists its files"
        ),
    },
    {
        "v": "2",
        "id": "vec-2",
        "from": "Grok Bot",
        "to": "Build",
        "lane": "lab",
        "priority": "urgent",
        "time": "2026-10-10 12:05",
        "routed": "2026-10-10 12:06",
        "pane": "2",
        "job": "list   lab    pane files",
        "done-when": "the pane lists its files",
        "canonical": (
            "v=2\nid=vec-2\nfrom=Grok Bot\nto=Build\nlane=lab\npriority=urgent\n"
            "time=2026-10-10 12:05\nrouted=2026-10-10 12:06\npane=2\n"
            "job=list lab pane files\ndone-when=the pane lists its files"
        ),
    },
    {
        "v": "2",
        "id": "vec-3",
        "from": "Lead",
        "to": "Build",
        "lane": "lab",
        "priority": "normal",
        "time": "2026-10-10 12:10",
        "job": "list lab pane files",
        "done-when": "the pane lists its files",
        "canonical": (
            "v=2\nid=vec-3\nfrom=Lead\nto=Build\nlane=lab\npriority=normal\n"
            "time=2026-10-10 12:10\nrouted=\npane=\n"
            "job=list lab pane files\ndone-when=the pane lists its files"
        ),
    },
    {
        "v": "2",
        "id": "vec-4",
        "from": "Lead",
        "to": "Build",
        "lane": "lab",
        "priority": "normal",
        "time": "2026-10-10 12:15",
        "routed": "2026-10-10 12:16",
        "job": "list lab pane files",
        "done-when": "the pane lists its files",
        "pane": "1",
        "canonical": (
            "v=2\nid=vec-4\nfrom=Lead\nto=Build\nlane=lab\npriority=normal\n"
            "time=2026-10-10 12:15\nrouted=2026-10-10 12:16\npane=1\n"
            "job=list lab pane files\ndone-when=the pane lists its files"
        ),
    },
)

VEC4_BLOCK = (
    "id: vec-4\nv: 2\nfrom: Lead\nto: Build\nlane: lab\npriority: normal\n"
    "time: 2026-10-10 12:15\nrouted: 2026-10-10 12:16\n"
    "job: list lab\npane files\ndone-when: the pane lists its files\npane: 1\n"
)

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


def _ps_shell():
    """Windows PowerShell 5.1 first; pwsh 7 is optional."""
    return shutil.which("powershell") or shutil.which("pwsh")


def _ps_argv(shell, script, *args):
    cmd = [str(shell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    cmd.extend(str(arg) for arg in args)
    return cmd


def _ps_detail(completed):
    return "exit %s; stdout=%s; stderr=%s" % (
        completed.returncode,
        (completed.stdout or "").strip(),
        (completed.stderr or "").strip(),
    )


def _write_utf8(path, text):
    """Write UTF-8 with no BOM. Preserves CR/LF already in text."""
    Path(path).write_bytes(text.encode("utf-8"))


def _run_ps(shell, script, args=(), env=None):
    return subprocess.run(
        _ps_argv(shell, script, *args),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env=env,
    )


def _extract_ps_function(text, name):
    token = f"function {name}"
    start = text.find(token)
    if start < 0:
        raise AssertionError("signer is missing a required function")
    brace = text.find("{", start)
    depth = 0
    for index in range(brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise AssertionError("signer function body is incomplete")


def _pwsh_mac(fields, key):
    script = ROOT / "tools" / "Sign-BusJob.ps1"
    text = script.read_text(encoding="utf-8")
    canonical_fn = _extract_ps_function(text, "Get-Canonical")
    mac_fn = _extract_ps_function(text, "Get-Mac")
    # Raw tab-separated fields (not ConvertFrom-Json). pwsh 7 parses ISO
    # strings in JSON as DateTime; 5.1 hashtables stay strings either way.
    rows = []
    for name in SIGNED_FIELDS:
        value = fields.get(name, "")
        if value is None:
            value = ""
        safe = str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")
        rows.append("%s\t%s" % (name, safe))
    body = (
        "param(\n"
        "    [Parameter(Mandatory = $true)][string]$PayloadPath,\n"
        "    [Parameter(Mandatory = $true)][string]$KeyPath\n"
        ")\n"
        "$ErrorActionPreference = \"Stop\"\n"
        "$fields = @('v','id','from','to','lane','priority','time','routed','pane','job','done-when')\n"
        f"{canonical_fn}\n{mac_fn}\n"
        "$utf8 = New-Object System.Text.UTF8Encoding $false\n"
        "$raw = [System.IO.File]::ReadAllText($PayloadPath, $utf8)\n"
        "$map = @{}\n"
        "foreach ($line in @($raw -split \"`n\")) {\n"
        "    $line = $line.TrimEnd(\"`r\")\n"
        "    $tab = $line.IndexOf([char]9)\n"
        "    if ($tab -lt 0) { continue }\n"
        "    $map[$line.Substring(0, $tab)] = [string]$line.Substring($tab + 1)\n"
        "}\n"
        "$secret = [System.Convert]::FromBase64String("
        "[System.IO.File]::ReadAllText($KeyPath, $utf8).Trim())\n"
        "try { Write-Output (Get-Mac $secret (Get-Canonical $map)) } "
        "finally { [Array]::Clear($secret, 0, $secret.Length) }\n"
    )
    helper = tempfile.NamedTemporaryFile("wb", suffix=".ps1", delete=False)
    payload_file = tempfile.NamedTemporaryFile("wb", suffix=".tsv", delete=False)
    key_file = tempfile.NamedTemporaryFile("wb", suffix=".b64", delete=False)
    try:
        helper.write(body.encode("utf-8"))
        helper.close()
        payload_file.write("\n".join(rows).encode("utf-8"))
        payload_file.close()
        key_file.write(base64.b64encode(bytes(key)))
        key_file.close()
        shell = _ps_shell()
        if not shell:
            raise AssertionError("PowerShell not on PATH")
        completed = _run_ps(
            shell,
            helper.name,
            ("-PayloadPath", payload_file.name, "-KeyPath", key_file.name),
        )
    finally:
        for path in (helper.name, payload_file.name, key_file.name):
            try:
                os.unlink(path)
            except OSError:
                pass
    if completed.returncode != 0:
        raise AssertionError("pwsh HMAC helper failed: %s" % _ps_detail(completed))
    lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    if not lines:
        raise AssertionError("pwsh HMAC helper returned no digest: %s" % _ps_detail(completed))
    return lines[-1]


class TestSigLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.bus = self.home / "bus"
        self.bus.mkdir()
        for name in ("LEAD-TO-BUILD.md", "GROKBOT-TO-BUILD.md", "BUILD-TO-LEAD.md", "BUILD-TO-GROKBOT.md"):
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
        iso = datetime.now(ZoneInfo("America/New_York")).replace(microsecond=0).isoformat()
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
        mac = sigverify.mac_hex(fields, self.keys[owner])
        self.secrets.append(mac)
        self.secrets.append(str(fields.get("job", "")))
        out = dict(fields)
        out["sig"] = mac
        return out

    def _write(self, fields, name="LEAD-TO-BUILD.md"):
        lines = [f"{key}: {fields[key]}" for key in WRITE_ORDER if key in fields]
        (self.bus / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _log_rows(self):
        if not server.LOG.is_file():
            return []
        rows = []
        for line in server.LOG.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def test_01_valid_signed_job_runs(self):
        signed = self._sign(self._job(id="ok-1"))
        self._write(signed)
        status, payload = server.apply_decision("ok-1", "approve")
        self.assertEqual(status, 200)
        self.assertEqual(self.reached, ["1"])
        decisions = [row.get("decision") for row in self._log_rows()]
        self.assertIn("approve", decisions)

    def test_02_unsigned_job_refused(self):
        self._write(self._job(id="nosig-1"))
        status, payload = server.apply_decision("nosig-1", "approve")
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "SPOOFED NO_SIG")
        self.assertEqual(self.reached, [])
        self._write(self._job(id="nosig-q"))
        held = [item for item in server.queue() if item.get("id") == "nosig-q"]
        self.assertTrue(held and held[0].get("status") == "held")
        rows = self._log_rows()
        self.assertTrue(any(row.get("decision") == "SPOOFED" and row.get("reason") == "NO_SIG" for row in rows))
        self.assertTrue(any(row.get("id") == "nosig-q" and row.get("decision") == "SPOOFED" for row in rows))

    def test_03_tampered_field_refused(self):
        base = self._sign(self._job(id="tamp-1"))
        for name in SIGNED_FIELDS:
            if name == "v":
                continue
            with self.subTest(name):
                tampered = dict(base)
                value = str(tampered.get(name, "x"))
                if not value:
                    value = "x"
                tampered[name] = value[:-1] + ("Q" if value[-1] != "Q" else "Z")
                reason = sigverify.verify_job(tampered, "Lead", seen=set())
                self.assertEqual(reason, "BAD_SIG")

    def test_04_wrong_owner_key_refused(self):
        fields = self._job(id="own-1", **{"from": "Grok Bot"})
        mac = sigverify.mac_hex(fields, self.keys["Lead"])
        self.secrets.append(mac)
        fields["sig"] = mac
        fields["pane"] = "1"
        reason = sigverify.verify_job(fields, "Lead", seen=set())
        self.assertEqual(reason, "OWNER_MISMATCH")
        self._write(fields)
        status, payload = server.apply_decision("own-1", "approve")
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "SPOOFED OWNER_MISMATCH")
        self.assertEqual(self.reached, [])

    def test_05_missing_key_refused(self):
        signed = self._sign(self._job(id="nokey-1"))
        sigverify.clear_key_provider()
        reason = sigverify.verify_job(signed, "Lead", seen=set())
        self.assertEqual(reason, "NO_KEY")
        self._write(signed)
        status, payload = server.apply_decision("nokey-1", "approve")
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "SPOOFED NO_KEY")
        self.assertEqual(self.reached, [])

    def test_06_approve_cannot_bypass(self):
        unsigned = self._job(id="force-1")
        self._write(unsigned)
        status, payload = server.apply_decision("force-1", "approve")
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "SPOOFED NO_SIG")
        code, _out, err = server.run_pane("1", unsigned.get("job", ""), unsigned, "Lead")
        self.assertEqual(code, 403)
        self.assertEqual(err, "NO_SIG")
        self.assertEqual(self.reached, [])
        reasons = [row.get("reason") for row in self._log_rows() if row.get("decision") == "SPOOFED"]
        self.assertGreaterEqual(reasons.count("NO_SIG"), 2)

    def test_07_replay_refused(self):
        signed = self._sign(self._job(id="rep-1"))
        self._write(signed)
        status, _payload = server.apply_decision("rep-1", "approve")
        self.assertEqual(status, 200)
        self.assertEqual(self.reached, ["1"])
        status2, payload2 = server.apply_decision("rep-1", "approve")
        self.assertEqual(status2, 403)
        self.assertEqual(payload2.get("error"), "SPOOFED REPLAY")
        self.assertEqual(self.reached, ["1"])
        self.assertTrue(any(row.get("decision") == "SPOOFED" and row.get("reason") == "REPLAY" for row in self._log_rows()))

    def test_08_cross_check(self):
        docs = (ROOT / "docs" / "SIGNING.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        opsec = (ROOT / "OPSEC.md").read_text(encoding="utf-8")
        script = (ROOT / "tools" / "Sign-BusJob.ps1").read_text(encoding="utf-8")
        for text in (docs, readme, opsec, script):
            self.assertIsNone(re.search(r"Parse\(\s*\"\d{4}-\d{2}-\d{2}T", text))
            self.assertIn(RULE13, text)
        self.assertIn("always strict", docs)
        self.assertIn("no date logic", docs)
        self.assertIn("no off switch", docs)
        self.assertIn("always strict", opsec)
        self.assertIn("no date logic", opsec)
        self.assertIn(RULE13, opsec)
        self.assertIn("always strict", readme)
        self.assertIn("no date logic", readme)
        self.assertIn("no off switch", readme)
        self.assertIn("pane` is optional", readme)
        self.assertIn("derives it from the signed", readme)
        self.assertIn("EnforceAt", script)
        self.assertIn("Test-SoftWarn", script)
        self.assertNotIn("HBC_TEST_KEY_PATH", script)
        self.assertIn("[a-z][a-z0-9-]*", script)
        self.assertIn("^[a-z][a-z0-9-]*: ?", docs)
        self.assertIn("format v2", docs)
        self.assertIn("pane` is signed", docs)
        self.assertIn("TEST-KEY", script)
        self.assertIn("InvariantCulture", script)
        self.assertIn("must be Grok Bot or Lead plus ISO-8601", script)
        self.assertIn("no trailing junk", script)
        self.assertIn("TEST-KEY", docs)
        self.assertIn("fills in `time`", docs)
        self.assertIn("There is no GB alias", docs)
        self.assertIn("BAD_PANE", docs)
        self.assertIn("never trims that field", docs)
        self.assertIn("refuses empty `routed`", docs)
        self.assertIn("Get-P4cRefuse", script)
        self.assertIn("BAD_FIELD", script)
        self.assertIn("BAD_VERB", script)
        self.assertIn("MISSING_FIELD", script)
        self.assertIn("OWNER_MISMATCH", script)
        self.assertIn("banned-verbs.txt", script)
        self.assertIn('"hub"', script)
        self.assertIn("proves nothing", docs)
        self.assertIn("proves nothing", opsec)
        self.assertIn("lowercase `key:` line inside the job text", docs)
        self.assertIn('"v"', script)
        self.assertIn('"pane"', script)
        verify_src = (ROOT / "hub" / "sigverify.py").read_text(encoding="utf-8")
        self.assertNotIn("EnforceAt", verify_src)
        self.assertNotIn("SPOOFED-WARN", verify_src)
        self.assertIn('FORMAT_VERSION = "2"', verify_src)
        self.assertIn("OLD_FORMAT", verify_src)

        for vector in GOLDEN:
            with self.subTest(vector["id"]):
                fields = {key: value for key, value in vector.items() if key != "canonical"}
                self.assertEqual(sigverify.canonicalize(fields), vector["canonical"])
                owner = sigverify.normalize_owner(fields["from"])
                signed = self._sign(fields, owner)
                reason = sigverify.verify_job(signed, owner, seen=set())
                self.assertEqual(reason, "")

        extra = self._sign(self._job(id="py-vec-1", lane="lab"))
        self.assertEqual(sigverify.verify_job(extra, "Lead", seen=set()), "")

        parsed = sigverify.parse_block_lines(VEC4_BLOCK.splitlines())
        self.assertEqual(parsed.get("pane"), "1")
        self.assertEqual(parsed.get("job"), "list lab pane files")
        self.assertEqual(parsed.get("done-when"), "the pane lists its files")
        self.assertEqual(sigverify.canonicalize(parsed), GOLDEN[-1]["canonical"])

    def test_08_pwsh_hmac(self):
        if not _ps_shell():
            self.skipTest("test 8 HMAC cross-check: powershell.exe (5.1) or pwsh not on PATH")
        for vector in GOLDEN:
            fields = {key: value for key, value in vector.items() if key != "canonical"}
            owner = sigverify.normalize_owner(fields["from"])
            left = sigverify.mac_hex(fields, self.keys[owner])
            right = _pwsh_mac(fields, self.keys[owner])
            self.secrets.extend([left, right])
            if left != right:
                self.fail("Python and Sign-BusJob.ps1 HMAC canonical logic disagree")
        extra = self._sign(self._job(id="py-vec-2", lane="lab"))
        right = _pwsh_mac({key: extra[key] for key in SIGNED_FIELDS}, self.keys["Lead"])
        self.secrets.append(right)
        if extra["sig"] != right:
            self.fail("Python-made vector does not match Sign-BusJob.ps1 HMAC canonical logic")

    def test_08_verify_direction(self):
        shell = _ps_shell()
        if os.name != "nt" or not shell:
            self.skipTest("test 8 -Verify direction: skipped on non-Windows (needs DPAPI)")
        signed = self._sign(self._job(id="ps-verify-1"))
        job_file = self.home / "job.txt"
        self._write_to(signed, job_file)
        wrapped = self._protect_bytes(shell, self.keys["Lead"])
        keys = self.home / "winhome" / ".grok" / "keys"
        keys.mkdir(parents=True)
        (keys / "bus-lead.bin").write_bytes(wrapped)
        env = os.environ.copy()
        env["USERPROFILE"] = str(self.home / "winhome")
        completed = _run_ps(
            shell,
            ROOT / "tools" / "Sign-BusJob.ps1",
            ("-Owner", "Lead", "-Id", "ps-verify-1", "-Verify", str(job_file)),
            env=env,
        )
        if completed.returncode != 0 or "OK " not in (completed.stdout or ""):
            self.fail(
                "Python-made vector did not verify with Sign-BusJob.ps1 -Verify: %s"
                % _ps_detail(completed)
            )

    def test_08_dpapi(self):
        if os.name != "nt":
            self.skipTest("test 8 real DPAPI unwrap: skipped on non-Windows")
        shell = _ps_shell()
        if not shell:
            self.skipTest("test 8 real DPAPI unwrap: skipped (PowerShell not on PATH)")
        wrapped = self._protect_bytes(shell, self.keys["Lead"])
        got = sigverify._unprotect_current_user(wrapped)
        try:
            if bytes(got) != bytes(self.keys["Lead"]):
                self.fail("in-process DPAPI unwrap did not match the test key")
        finally:
            sigverify._wipe(got)

    def _write_to(self, fields, path):
        lines = [f"{key}: {fields[key]}" for key in WRITE_ORDER if key in fields]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _protect_bytes(self, shell, key):
        script = (
            "param([Parameter(Mandatory = $true)][string]$KeyPath)\n"
            "$ErrorActionPreference = \"Stop\"\n"
            "Add-Type -AssemblyName System.Security\n"
            "$utf8 = New-Object System.Text.UTF8Encoding $false\n"
            "$raw = [System.IO.File]::ReadAllText($KeyPath, $utf8)\n"
            "$plain = [System.Convert]::FromBase64String($raw.Trim())\n"
            "$prot = [System.Security.Cryptography.ProtectedData]::Protect("
            "$plain, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)\n"
            "Write-Output ([System.Convert]::ToBase64String($prot))\n"
        )
        helper = self.home / "protect.ps1"
        key_file = self.home / "protect-key.b64"
        _write_utf8(helper, script)
        _write_utf8(key_file, base64.b64encode(bytes(key)).decode("ascii"))
        completed = _run_ps(shell, helper, ("-KeyPath", str(key_file)))
        if completed.returncode != 0:
            raise AssertionError("DPAPI protect helper failed: %s" % _ps_detail(completed))
        lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
        if not lines:
            raise AssertionError("DPAPI protect helper returned no blob: %s" % _ps_detail(completed))
        return base64.b64decode(lines[-1])

    def test_09_settings_cannot_disable(self):
        names = ("HBC_SKIP_SIGVERIFY", "HBC_DISABLE_SIGVERIFY", "HBC_SIGVERIFY", "HBC_ENFORCE_AT")
        previous = {name: os.environ.get(name) for name in names}
        future = _shift_iso(2)
        os.environ["HBC_SKIP_SIGVERIFY"] = "1"
        os.environ["HBC_DISABLE_SIGVERIFY"] = "true"
        os.environ["HBC_SIGVERIFY"] = "0"
        os.environ["HBC_ENFORCE_AT"] = future
        try:
            sigverify.ignore_disable_settings(argv=["--skip-sigverify", "--enforce-at", future])
            self._write(self._job(id="flag-1"))
            status, payload = server.apply_decision("flag-1", "approve")
            self.assertEqual(status, 403)
            self.assertEqual(payload.get("error"), "SPOOFED NO_SIG")
            self.assertEqual(self.reached, [])
            self.assertEqual(sigverify.verify_job(self._job(id="flag-2"), "Lead", seen=set()), "NO_SIG")
            self._write(self._job(id="flag-3"))
            status3, payload3 = server.apply_decision("flag-3", "approve")
            self.assertEqual(status3, 403)
            self.assertEqual(payload3.get("error"), "SPOOFED NO_SIG")
            self.assertEqual(self.reached, [])
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_11_script_default(self):
        shell = _ps_shell()
        if not shell:
            self.skipTest("test 11 script default: PowerShell not on PATH")
        raw = self.home / "test-lead.key"
        raw.write_bytes(bytes(self.keys["Lead"]))
        signed = self._sign(self._job(id="scr-1"))
        bad = dict(signed)
        bad["sig"] = "0" * 64
        job_file = self.home / "script-job.txt"
        self._write_to(bad, job_file)

        default = self._run_verify(shell, job_file, "scr-1", raw)
        if default.returncode != 3 or "SPOOFED-WARN" in (default.stdout or "") or "SPOOFED" not in (default.stdout or ""):
            self.fail("script default did not refuse a bad signature: %s" % _ps_detail(default))
        if "TEST-KEY" not in (default.stdout or ""):
            self.fail("script did not print TEST-KEY for -TestKeyPath: %s" % _ps_detail(default))

        unread = self._run_verify(shell, job_file, "scr-1", raw, enforce_at="not-an-iso-time")
        if unread.returncode != 3 or "SPOOFED-WARN" in (unread.stdout or "") or "SPOOFED" not in (unread.stdout or ""):
            self.fail("unreadable enforceAt did not enforce now: %s" % _ps_detail(unread))

        past = self._run_verify(shell, job_file, "scr-1", raw, enforce_at=_shift_iso(-2))
        if past.returncode != 3 or "SPOOFED-WARN" in (past.stdout or "") or "SPOOFED" not in (past.stdout or ""):
            self.fail("past enforceAt did not enforce now: %s" % _ps_detail(past))

        future = self._run_verify(shell, job_file, "scr-1", raw, enforce_at=_shift_iso(2))
        if future.returncode != 0 or "SPOOFED-WARN" not in (future.stdout or ""):
            self.fail("future enforceAt did not soft-warn on the script: %s" % _ps_detail(future))

    def _ps_env(self):
        env = os.environ.copy()
        env.pop("HBC_TEST_KEY_PATH", None)
        env["TEMP"] = str(self.home)
        return env

    def _run_verify(self, shell, job_file, job_id, raw_key, enforce_at=None):
        args = [
            "-Owner", "Lead", "-Id", job_id, "-Verify",
            "-TestKeyPath", str(raw_key), str(job_file),
        ]
        if enforce_at is not None:
            args.extend(["-EnforceAt", enforce_at])
        return _run_ps(shell, ROOT / "tools" / "Sign-BusJob.ps1", args, env=self._ps_env())

    def _run_sign(self, shell, job_file, job_id, raw_key):
        args = [
            "-Owner", "Lead", "-Id", job_id,
            "-TestKeyPath", str(raw_key), str(job_file),
        ]
        return _run_ps(shell, ROOT / "tools" / "Sign-BusJob.ps1", args, env=self._ps_env())

    def test_08_script_signed_job_runs(self):
        shell = _ps_shell()
        if not shell:
            self.skipTest("test 8 script-signed e2e: PowerShell not on PATH")
        raw = self.home / "e2e-lead.key"
        raw.write_bytes(bytes(self.keys["Lead"]))
        job_file = self.home / "e2e-job.txt"
        job_file.write_text(
            "id: e2e-1\nv: 2\nfrom: Lead\nto: Build\nlane: lab\npriority: normal\n"
            "time: 2026-10-10T12:00:00-04:00\nrouted: Lead 2026-10-10T12:00:00-04:00\n"
            "job: list lab\npane files\ndone-when: the pane lists its files\npane: 1\n",
            encoding="utf-8",
        )
        signed = self._run_sign(shell, job_file, "e2e-1", raw)
        if signed.returncode != 0 or "SIGNED" not in (signed.stdout or ""):
            self.fail("Sign-BusJob.ps1 did not sign the full job block: %s" % _ps_detail(signed))
        text = job_file.read_text(encoding="utf-8")
        parsed_list = server.blocks(text)
        if not parsed_list:
            self.fail("hub did not parse the script-signed block")
        parsed = parsed_list[0]
        self.assertEqual(parsed.get("pane"), "1")
        self.assertEqual(parsed.get("job"), "list lab pane files")
        if "pane:" in parsed.get("done-when", ""):
            self.fail("pane was folded into done-when")
        reason = sigverify.verify_job(parsed, "Lead", seen=set())
        self.assertEqual(reason, "")
        if parsed.get("sig"):
            self.secrets.append(parsed["sig"])
        (self.bus / "LEAD-TO-BUILD.md").write_text(text, encoding="utf-8")
        status, payload = server.apply_decision("e2e-1", "approve")
        self.assertEqual(status, 200)
        self.assertEqual(self.reached, ["1"])
        self.assertIn("approve", [row.get("decision") for row in self._log_rows()])
        self.assertEqual(parsed.get("v"), "2")

    def test_12_pane_tamper_refused(self):
        signed = self._sign(self._job(id="pane-tamp-1", pane="1"))
        tampered = dict(signed)
        tampered["pane"] = "2"
        reason = sigverify.verify_job(tampered, "Lead", seen=set())
        self.assertEqual(reason, "BAD_SIG")
        self._write(tampered)
        status, payload = server.apply_decision("pane-tamp-1", "approve")
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "SPOOFED BAD_SIG")
        self.assertEqual(self.reached, [])
        self.assertTrue(any(
            row.get("decision") == "SPOOFED" and row.get("reason") == "BAD_SIG"
            for row in self._log_rows()
        ))

    def test_13_v1_signed_job_old_format(self):
        fields = self._job(id="old-1")
        fields.pop("v", None)
        mac = self._v1_mac(fields, self.keys["Lead"])
        self.secrets.append(mac)
        fields["sig"] = mac
        reason = sigverify.verify_job(fields, "Lead", seen=set())
        self.assertEqual(reason, "OLD_FORMAT")
        self._write(fields)
        status, payload = server.apply_decision("old-1", "approve")
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "SPOOFED OLD_FORMAT")
        self.assertEqual(self.reached, [])
        self.assertTrue(any(
            row.get("decision") == "SPOOFED" and row.get("reason") == "OLD_FORMAT"
            for row in self._log_rows()
        ))

    def test_14_pane_override_refused(self):
        signed = self._sign(self._job(id="ovr-1", pane="1"))
        code, _out, err = server.run_pane("2", signed.get("job", ""), signed, "Lead")
        self.assertEqual(code, 403)
        self.assertEqual(err, "PANE_MISMATCH")
        self.assertEqual(self.reached, [])
        mapped = self._sign(self._job(id="ovr-2", pane=""))
        code2, _out2, err2 = server.run_pane("2", mapped.get("job", ""), mapped, "Lead")
        self.assertEqual(code2, 403)
        self.assertEqual(err2, "PANE_MISMATCH")
        self.assertEqual(self.reached, [])
        code3, _out3, err3 = server.run_pane("1", mapped.get("job", ""), mapped, "Lead")
        self.assertEqual(code3, 0)
        self.assertEqual(err3, "")
        self.assertEqual(self.reached, ["1"])
        self.assertTrue(any(
            row.get("decision") == "SPOOFED" and row.get("reason") == "PANE_MISMATCH"
            for row in self._log_rows()
        ))

    def test_b1_heading_terminator_script_and_hub_agree(self):
        shell = _ps_shell()
        if not shell:
            self.skipTest("heading terminator agreement: PowerShell not on PATH")
        text = (
            "id: agree-1\nv: 2\nfrom: Lead\nto: Build\nlane: lab\n"
            "priority: normal\ntime: 2026-10-10 12:00\nrouted: 2026-10-10 12:01\n"
            "pane: 1\njob: list lab\npane files\ndone-when: the pane lists its files\n"
            "## Notes\nfrom: Attacker\njob: should not be in the first block\n"
        )
        hub_blocks = server.blocks(text)
        self.assertTrue(hub_blocks)
        hub = hub_blocks[0]
        script = self._pwsh_block_map(shell, text, "agree-1")
        for name in ("v", "id", "from", "to", "lane", "priority", "time", "routed", "pane", "job", "done-when"):
            self.assertEqual(hub.get(name, ""), script.get(name, ""), name)
        self.assertEqual(hub.get("from"), "Lead")
        self.assertEqual(hub.get("job"), "list lab pane files")
        self.assertNotEqual(hub.get("from"), "Attacker")
        self.assertNotIn("should not be in the first block", hub.get("job", ""))

    def test_b1_crlf_split_script_and_hub_agree(self):
        shell = _ps_shell()
        if not shell:
            self.skipTest("CRLF split agreement: PowerShell not on PATH")
        text = (
            "id: agree-crlf\nv: 2\nfrom: Lead\nto: Build\nlane: lab\n"
            "priority: normal\ntime: 2026-10-10 12:00\nrouted: 2026-10-10 12:01\n"
            "pane: 1\njob: list lab\npane files\ndone-when: the pane lists its files\n"
        ).replace("\n", "\r\n")
        hub_blocks = server.blocks(text)
        self.assertTrue(hub_blocks)
        hub = hub_blocks[0]
        script = self._pwsh_block_map(shell, text, "agree-crlf")
        for name in ("v", "id", "from", "to", "lane", "priority", "time", "routed", "pane", "job", "done-when"):
            self.assertEqual(hub.get(name, ""), script.get(name, ""), name)
        self.assertEqual(hub.get("job"), "list lab pane files")
        self.assertEqual(hub.get("done-when"), "the pane lists its files")
        self.assertEqual(hub.get("pane"), "1")

    def test_b1_lone_cr_job_pane_agrees(self):
        shell = _ps_shell()
        if not shell:
            self.skipTest("lone CR agreement: PowerShell not on PATH")
        text = (
            "id: cr-1\nv: 2\nfrom: Lead\nto: Build\nlane: lab\n"
            "priority: normal\ntime: 2026-10-10 12:00\nrouted: 2026-10-10 12:01\n"
            "job: x\rpane: 2\ndone-when: the pane lists its files\n"
        )
        path = self.bus / "LEAD-TO-BUILD.md"
        path.write_bytes(text.encode("utf-8"))
        loaded = server.read_bus(path)
        self.assertIn("\r", loaded)
        hub_blocks = server.blocks(loaded)
        self.assertTrue(hub_blocks)
        hub = hub_blocks[0]
        script = self._pwsh_block_map(shell, loaded, "cr-1")
        for name in ("v", "id", "from", "to", "lane", "priority", "time", "routed", "pane", "job", "done-when"):
            self.assertEqual(hub.get(name, ""), script.get(name, ""), name)
        self.assertEqual(hub.get("pane", ""), "")
        self.assertIn("x", hub.get("job", ""))
        self.assertIn("pane: 2", hub.get("job", "").replace("\r", " "))

    def test_bad_pane_logged_and_held(self):
        signed = self._sign(self._job(id="badpane-1", pane="3"))
        self._write(signed)
        held = [item for item in server.queue() if item.get("id") == "badpane-1"]
        self.assertTrue(held and held[0].get("status") == "held")
        self.assertEqual(held[0].get("reason"), "BAD_PANE")
        self.assertEqual(self.reached, [])
        rows = [row for row in self._log_rows() if row.get("id") == "badpane-1"]
        self.assertTrue(any(row.get("decision") == "held" and row.get("reason") == "BAD_PANE" for row in rows))
        for row in rows:
            if "sig" in row or "key" in row or "job" in row or "body" in row:
                self.fail("pane refusal log has a forbidden field")
            if "list lab pane files" in json.dumps(row):
                self.fail("pane refusal log has a job body")
        status, payload = server.apply_decision("badpane-1", "approve")
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "BAD_PANE")
        self.assertEqual(self.reached, [])

    def test_unmapped_route_logged_and_held(self):
        signed = self._sign(self._job(id="unmap-1", pane="", to="Other", lane="none"))
        self._write(signed)
        held = [item for item in server.queue() if item.get("id") == "unmap-1"]
        self.assertTrue(held and held[0].get("status") == "held")
        self.assertEqual(held[0].get("reason"), "UNMAPPED")
        self.assertEqual(self.reached, [])
        rows = [row for row in self._log_rows() if row.get("id") == "unmap-1"]
        self.assertTrue(any(row.get("decision") == "held" and row.get("reason") == "UNMAPPED" for row in rows))
        for row in rows:
            if "sig" in row or "key" in row or "job" in row or "body" in row:
                self.fail("unmapped refusal log has a forbidden field")
            if "list lab pane files" in json.dumps(row):
                self.fail("unmapped refusal log has a job body")
        status, payload = server.apply_decision("unmap-1", "approve")
        self.assertEqual(status, 403)
        self.assertEqual(payload.get("error"), "UNMAPPED")
        self.assertEqual(self.reached, [])

    def test_decide_missing_return_creates_file(self):
        signed = self._sign(self._job(id="no-ret"))
        self._write(signed)
        ret = self.bus / "BUILD-TO-LEAD.md"
        if ret.is_file():
            ret.unlink()
        status, payload = server.apply_decision("no-ret", "approve")
        self.assertNotEqual(status, 500)
        self.assertLess(status, 500)
        self.assertIsInstance(payload, dict)
        self.assertTrue(ret.is_file())
        self.assertEqual(status, 200)
        self.assertIn("VERDICT:", ret.read_text(encoding="utf-8"))

    def test_decide_return_write_failure_is_4xx(self):
        self._write(self._job(id="ret-fail"))
        with mock.patch("server.append_return", side_effect=FileNotFoundError("missing")):
            status, payload = server.apply_decision("ret-fail", "approve")
        self.assertGreaterEqual(status, 400)
        self.assertLess(status, 500)
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload.get("error"), "RETURN")
        self.assertEqual(self.reached, [])

    def test_append_return_keeps_earlier_contents(self):
        ret = self.bus / "BUILD-TO-LEAD.md"
        ret.write_text("FIRST\n", encoding="utf-8")
        real_read = Path.read_text
        errors = []

        def gated_read(self, *args, **kwargs):
            text = real_read(self, *args, **kwargs)
            if self.name == "BUILD-TO-LEAD.md":
                time.sleep(0.05)
            return text

        def worker(tag):
            try:
                server.append_return("Lead", tag)
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(Path, "read_text", gated_read):
            first = threading.Thread(target=worker, args=("AAA-KEEP",))
            second = threading.Thread(target=worker, args=("BBB-KEEP",))
            first.start()
            second.start()
            first.join()
            second.join()
        self.assertEqual(errors, [])
        text = ret.read_text(encoding="utf-8")
        self.assertIn("FIRST", text)
        self.assertIn("AAA-KEEP", text)
        self.assertIn("BBB-KEEP", text)

    def test_failed_swap_after_run_is_not_403(self):
        signed = self._sign(self._job(id="swap-fail"))
        self._write(signed)
        real_replace = server._os_replace

        def boom(src, dst):
            if str(dst).endswith("BUILD-TO-LEAD.md"):
                raise OSError("swap failed")
            return real_replace(src, dst)

        with mock.patch.object(server, "_os_replace", boom):
            status, payload = server.apply_decision("swap-fail", "approve")
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("result"), "pane-ok")
        self.assertEqual(payload.get("exit"), 0)
        self.assertEqual(self.reached, ["1"])
        rows = self._log_rows()
        self.assertTrue(any(
            row.get("id") == "swap-fail" and row.get("decision") == "approve"
            for row in rows
        ))
        self.assertTrue(any(
            row.get("id") == "swap-fail"
            and row.get("decision") == "alarm"
            and row.get("reason") == "RETURN"
            for row in rows
        ))
        rec = protocol.find_seen(server.SEEN, "swap-fail", alarm_claims=False)
        self.assertTrue(rec)
        self.assertEqual(payload.get("return"), "ALARM")
        reply = payload.get("reply")
        self.assertIsInstance(reply, str)
        self.assertIn("VERDICT: RAN", reply)
        self.assertIn("id: swap-fail", reply)
        self.assertIn("result: pane-ok", reply)
        ret = self.bus / "BUILD-TO-LEAD.md"
        body = ret.read_text(encoding="utf-8") if ret.is_file() else ""
        self.assertNotIn("pane-ok", body)
        self.assertNotIn("VERDICT: RAN", body)
        server.append_return("Lead", "id: flush-1\n")
        flushed = ret.read_text(encoding="utf-8")
        self.assertIn("VERDICT: RAN", flushed)
        self.assertIn("id: swap-fail", flushed)
        self.assertIn("result: pane-ok", flushed)
        self.assertIn("id: flush-1", flushed)

    def test_queued_ran_reply_flushes_on_next_write(self):
        signed = self._sign(self._job(id="queue-ran"))
        self._write(signed)
        real_replace = server._os_replace

        def boom(src, dst):
            if str(dst).endswith("BUILD-TO-LEAD.md"):
                raise OSError("swap failed")
            return real_replace(src, dst)

        with mock.patch.object(server, "_os_replace", boom):
            status, payload = server.apply_decision("queue-ran", "approve")
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("return"), "ALARM")
        self.assertIn("VERDICT: RAN", payload.get("reply", ""))
        ret = self.bus / "BUILD-TO-LEAD.md"
        self.assertNotIn("VERDICT: RAN", ret.read_text(encoding="utf-8") if ret.is_file() else "")
        later = self._sign(self._job(id="queue-next", job="list lab pane files next"))
        self._write(later)
        status2, payload2 = server.apply_decision("queue-next", "approve")
        self.assertEqual(status2, 200)
        self.assertNotEqual(payload2.get("return"), "ALARM")
        text = ret.read_text(encoding="utf-8")
        self.assertIn("id: queue-ran", text)
        self.assertIn("id: queue-next", text)
        self.assertGreaterEqual(text.count("VERDICT: RAN"), 2)

    def test_pending_returns_survive_restart(self):
        signed = self._sign(self._job(id="restart-q"))
        self._write(signed)
        real_replace = server._os_replace

        def boom(src, dst):
            if str(dst).endswith("BUILD-TO-LEAD.md"):
                raise OSError("swap failed")
            return real_replace(src, dst)

        with mock.patch.object(server, "_os_replace", boom):
            status, payload = server.apply_decision("restart-q", "approve")
        self.assertEqual(status, 200)
        self.assertEqual(payload.get("return"), "ALARM")
        pending = server._pending_path("Lead")
        self.assertTrue(pending.is_file())
        queued = json.loads(pending.read_text(encoding="utf-8"))
        self.assertTrue(any("id: restart-q" in str(item) for item in queued))
        loaded = server.load_pending_returns()
        self.assertTrue(any("id: restart-q" in str(item) for item in loaded.get("Lead", [])))
        server.append_return("Lead", "id: after-restart\n")
        ret = self.bus / "BUILD-TO-LEAD.md"
        text = ret.read_text(encoding="utf-8")
        self.assertIn("id: restart-q", text)
        self.assertIn("VERDICT: RAN", text)
        self.assertIn("id: after-restart", text)
        if pending.is_file():
            leftover = json.loads(pending.read_text(encoding="utf-8"))
            self.assertEqual(leftover, [])

    def test_pending_returns_keep_write_order(self):
        ret = self.bus / "BUILD-TO-LEAD.md"
        ret.write_text("", encoding="utf-8")
        real_replace = server._os_replace

        def boom(src, dst):
            if str(dst).endswith("BUILD-TO-LEAD.md"):
                raise OSError("swap failed")
            return real_replace(src, dst)

        with mock.patch.object(server, "_os_replace", boom):
            with self.assertRaises(OSError):
                server.append_return("Lead", "FIRST")
            with self.assertRaises(OSError):
                server.append_return("Lead", "SECOND")
        server.append_return("Lead", "THIRD")
        text = ret.read_text(encoding="utf-8")
        self.assertLess(text.index("FIRST"), text.index("SECOND"))
        self.assertLess(text.index("SECOND"), text.index("THIRD"))

    def test_decide_bad_json_is_400(self):
        prev = server.TOKEN
        try:
            server.TOKEN = "a" * 32

            class BadJson:
                path = "/api/decide"
                query = {}
                headers = {"X-Hbc-Token": server.TOKEN}

                async def json(self):
                    raise ValueError("not json")

            class NotDict:
                path = "/api/decide"
                query = {}
                headers = {"X-Hbc-Token": server.TOKEN}

                async def json(self):
                    return ["not", "an", "object"]

            async def run(req):
                resp = await server.decide(req)
                body = resp.body
                if isinstance(body, (bytes, bytearray)):
                    payload = json.loads(body.decode("utf-8"))
                else:
                    payload = json.loads(body)
                return resp.status, payload

            status, payload = asyncio.run(run(BadJson()))
            self.assertEqual(status, 400)
            self.assertEqual(payload.get("error"), "bad json")
            status, payload = asyncio.run(run(NotDict()))
            self.assertEqual(status, 400)
            self.assertEqual(payload.get("error"), "bad json")
        finally:
            server.TOKEN = prev

    def _v1_mac(self, fields, key):
        lines = []
        for name in V1_FIELDS:
            raw = fields.get(name, "")
            if raw is None:
                raw = ""
            value = sigverify.SPACE.sub(" ", str(raw).strip())
            lines.append(f"{name}={value}")
        digest = hmac.new(bytes(key), "\n".join(lines).encode("utf-8"), hashlib.sha256).hexdigest()
        return digest

    def _pwsh_block_map(self, shell, text, job_id):
        src = (ROOT / "tools" / "Sign-BusJob.ps1").read_text(encoding="utf-8")
        get_block = _extract_ps_function(src, "Get-BlockMap")
        body = (
            "param(\n"
            "    [Parameter(Mandatory = $true)][string]$Id,\n"
            "    [Parameter(Mandatory = $true)][string]$JobFile\n"
            ")\n"
            "$ErrorActionPreference = \"Stop\"\n"
            f"{get_block}\n"
            "$utf8 = New-Object System.Text.UTF8Encoding $false\n"
            "$text = [System.IO.File]::ReadAllText($JobFile, $utf8)\n"
            "$lines = $text -split \"`r?`n\"\n"
            "$start = -1\n"
            "for ($i = 0; $i -lt $lines.Length; $i++) {\n"
            "    if ($lines[$i] -match (\"^id: \" + [regex]::Escape($Id) + \"$\")) { $start = $i; break }\n"
            "}\n"
            "if ($start -lt 0) { throw \"job id not found\" }\n"
            "$end = $lines.Length\n"
            "for ($i = $start + 1; $i -lt $lines.Length; $i++) {\n"
            "    if ($lines[$i] -match \"^(id: |## )\") { $end = $i; break }\n"
            "}\n"
            "$block = @($lines[$start..($end - 1)])\n"
            "$map = Get-BlockMap $block\n"
            "$out = New-Object System.Collections.Hashtable\n"
            "foreach ($entry in $map.GetEnumerator()) {\n"
            "    $out[$entry.Key] = [string]$entry.Value\n"
            "}\n"
            "ConvertTo-Json -InputObject $out -Compress -Depth 5\n"
        )
        helper = self.home / ("block-%s.ps1" % job_id)
        job_path = self.home / ("block-%s.txt" % job_id)
        _write_utf8(helper, body)
        _write_utf8(job_path, text)
        completed = _run_ps(shell, helper, ("-Id", job_id, "-JobFile", str(job_path)))
        if completed.returncode != 0:
            raise AssertionError("pwsh block-map helper failed: %s" % _ps_detail(completed))
        lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
        if not lines:
            raise AssertionError("pwsh block-map helper returned no JSON: %s" % _ps_detail(completed))
        parsed = json.loads(lines[-1])
        return {str(key): str(value) for key, value in parsed.items()}

    def test_b1_crlf_real_signer_ps51(self):
        if os.name != "nt":
            self.skipTest(
                "test_b1_crlf_real_signer_ps51: Windows-only "
                "(needs Windows PowerShell 5.1 / powershell.exe)"
            )
        shell = shutil.which("powershell")
        if not shell:
            self.skipTest(
                "test_b1_crlf_real_signer_ps51: powershell.exe "
                "(Windows PowerShell 5.1) not on PATH"
            )
        job_id = "ps51-1"
        text = (
            "id: ps51-1\r\n"
            "v: 2\r\n"
            "from: Lead\r\n"
            "to: Build\r\n"
            "lane: lab\r\n"
            "priority: normal\r\n"
            "time: 2026-10-10 12:00\r\n"
            "routed: Lead 2026-10-10T12:00:00-04:00\r\n"
            "job: list lab\r\n"
            "pane files\r\n"
            "done-when: the pane lists its files\r\n"
            "pane: 1\r\n"
        )
        job_file = self.home / "ps51-job.txt"
        job_file.write_bytes(text.encode("utf-8"))
        raw = self.home / "ps51-lead.key"
        raw.write_bytes(bytes(self.keys["Lead"]))
        signed = self._run_sign(shell, job_file, job_id, raw)
        if signed.returncode != 0 or "SIGNED" not in (signed.stdout or ""):
            self.fail(
                "Sign-BusJob.ps1 under powershell.exe 5.1 did not sign a CRLF job: %s"
                % _ps_detail(signed)
            )
        data = job_file.read_bytes()
        if data.startswith(b"\xef\xbb\xbf"):
            self.fail("Sign-BusJob.ps1 wrote a UTF-8 BOM under Windows PowerShell 5.1")
        parsed_list = server.blocks(data.decode("utf-8"))
        if not parsed_list:
            self.fail("hub did not parse the 5.1-signed CRLF block")
        parsed = parsed_list[0]
        self.assertEqual(parsed.get("job"), "list lab pane files")
        self.assertEqual(parsed.get("pane"), "1")
        self.assertEqual(parsed.get("v"), "2")
        if "pane:" in parsed.get("done-when", ""):
            self.fail("pane was folded into done-when")
        reason = sigverify.verify_job(parsed, "Lead", seen=set())
        self.assertEqual(reason, "")
        if parsed.get("sig"):
            self.secrets.append(parsed["sig"])
        verified = self._run_verify(shell, job_file, job_id, raw)
        if verified.returncode != 0 or "OK " not in (verified.stdout or ""):
            self.fail(
                "Sign-BusJob.ps1 -Verify under powershell.exe 5.1 did not agree: %s"
                % _ps_detail(verified)
            )

    def test_signer_invariant_culture_fi_FI(self):
        script = (ROOT / "tools" / "Sign-BusJob.ps1").read_text(encoding="utf-8")
        if "InvariantCulture" not in script:
            self.fail("signer does not format ISO time with InvariantCulture")
        zone_fn = _extract_ps_function(script, "Get-EasternZone")
        iso_fn = _extract_ps_function(script, "Get-EasternIsoNow")
        shell = _ps_shell()
        if not shell:
            self.skipTest("fi-FI signer check: PowerShell not on PATH")
        body = (
            "$ErrorActionPreference = \"Stop\"\n"
            f"{zone_fn}\n{iso_fn}\n"
            "$fi = [System.Globalization.CultureInfo]::GetCultureInfo('fi-FI')\n"
            "[System.Threading.Thread]::CurrentThread.CurrentCulture = $fi\n"
            "[System.Threading.Thread]::CurrentThread.CurrentUICulture = $fi\n"
            "Write-Output (Get-EasternIsoNow)\n"
        )
        helper = self.home / "fi-iso.ps1"
        _write_utf8(helper, body)
        completed = _run_ps(shell, helper, ())
        if completed.returncode != 0:
            self.fail("fi-FI ISO helper failed: %s" % _ps_detail(completed))
        lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
        if not lines:
            self.fail("fi-FI ISO helper returned no time")
        value = lines[-1]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", value) is None:
            self.fail("fi-FI culture changed the ISO time format: %s" % value)

    def test_signer_refuses_time_without_space(self):
        shell = _ps_shell()
        if not shell:
            self.skipTest("signer refuse tests: PowerShell not on PATH")
        raw = self.home / "refuse-lead.key"
        raw.write_bytes(bytes(self.keys["Lead"]))
        job_file = self.home / "refuse-time.txt"
        job_file.write_text(
            "id: nospace-1\nv: 2\nfrom: Lead\nto: Build\nlane: lab\npriority: normal\n"
            "time:2026-10-10T12:00:00-04:00\n"
            "routed: Lead 2026-10-10T12:00:00-04:00\n"
            "job: list lab pane files\ndone-when: the pane lists its files\npane: 1\n",
            encoding="utf-8",
        )
        signed = self._run_sign(shell, job_file, "nospace-1", raw)
        if signed.returncode != 3:
            self.fail("time: with no space did not exit 3: %s" % _ps_detail(signed))
        if "SIGNED" in (signed.stdout or ""):
            self.fail("signer printed SIGNED for a time: line with no space")
        if "space" not in (signed.stdout or "").lower():
            self.fail("signer did not explain the missing space: %s" % _ps_detail(signed))

    def test_signer_refuses_time_continuation(self):
        shell = _ps_shell()
        if not shell:
            self.skipTest("signer refuse tests: PowerShell not on PATH")
        raw = self.home / "refuse-cont.key"
        raw.write_bytes(bytes(self.keys["Lead"]))
        job_file = self.home / "refuse-cont.txt"
        job_file.write_text(
            "id: cont-1\nv: 2\nfrom: Lead\nto: Build\nlane: lab\npriority: normal\n"
            "time: 2026-10-10T12:00:00-04:00\n extra\n"
            "routed: Lead 2026-10-10T12:00:00-04:00\n"
            "job: list lab pane files\ndone-when: the pane lists its files\npane: 1\n",
            encoding="utf-8",
        )
        signed = self._run_sign(shell, job_file, "cont-1", raw)
        if signed.returncode != 3:
            self.fail("time continuation did not exit 3: %s" % _ps_detail(signed))
        if "SIGNED" in (signed.stdout or ""):
            self.fail("signer printed SIGNED for a time continuation")
        if "BAD_FIELD" not in (signed.stdout or ""):
            self.fail("signer did not print BAD_FIELD: %s" % _ps_detail(signed))

    def test_signer_refuses_pane_continuation(self):
        shell = _ps_shell()
        if not shell:
            self.skipTest("signer refuse tests: PowerShell not on PATH")
        raw = self.home / "refuse-pane-cont.key"
        raw.write_bytes(bytes(self.keys["Lead"]))
        job_file = self.home / "refuse-pane-cont.txt"
        job_file.write_text(
            "id: pane-cont\nv: 2\nfrom: Lead\nto: Build\nlane: lab\npriority: normal\n"
            "time: 2026-10-10T12:00:00-04:00\n"
            "routed: Lead 2026-10-10T12:00:00-04:00\n"
            "job: list lab pane files\ndone-when: the pane lists its files\n"
            "pane: 1\n extra\n",
            encoding="utf-8",
        )
        signed = self._run_sign(shell, job_file, "pane-cont", raw)
        if signed.returncode != 3:
            self.fail("pane continuation did not exit 3: %s" % _ps_detail(signed))
        if "SIGNED" in (signed.stdout or ""):
            self.fail("signer printed SIGNED for a pane continuation")
        if "BAD_FIELD" not in (signed.stdout or ""):
            self.fail("signer did not print BAD_FIELD: %s" % _ps_detail(signed))

    def test_signer_refuses_routed_trailing_junk(self):
        shell = _ps_shell()
        if not shell:
            self.skipTest("signer trailing-junk test: PowerShell not on PATH")
        raw = self.home / "junk-lead.key"
        raw.write_bytes(bytes(self.keys["Lead"]))
        job_file = self.home / "junk-router.txt"
        job_file.write_text(
            "id: junk-router\nv: 2\nfrom: Lead\nto: Build\nlane: lab\npriority: normal\n"
            "time: 2026-10-10T12:00:00-04:00\n"
            "routed: Lead 2026-10-10T12:00:00-04:00 extra\n"
            "job: list lab pane files\ndone-when: the pane lists its files\npane: 1\n",
            encoding="utf-8",
        )
        signed = self._run_sign(shell, job_file, "junk-router", raw)
        if signed.returncode != 3:
            self.fail("trailing junk on routed did not exit 3: %s" % _ps_detail(signed))
        if "SIGNED" in (signed.stdout or ""):
            self.fail("signer printed SIGNED for trailing junk on routed")

    def test_signer_refuses_router_owner_mismatch(self):
        shell = _ps_shell()
        if not shell:
            self.skipTest("signer owner-mismatch test: PowerShell not on PATH")
        raw = self.home / "mismatch-lead.key"
        raw.write_bytes(bytes(self.keys["Lead"]))
        job_file = self.home / "mismatch-router.txt"
        job_file.write_text(
            "id: mismatch-router\nv: 2\nfrom: Lead\nto: Build\nlane: lab\npriority: normal\n"
            "time: 2026-10-10T12:00:00-04:00\n"
            "routed: Grok Bot 2026-10-10T12:00:00-04:00\n"
            "job: list lab pane files\ndone-when: the pane lists its files\npane: 1\n",
            encoding="utf-8",
        )
        signed = self._run_sign(shell, job_file, "mismatch-router", raw)
        if signed.returncode != 3:
            self.fail("router/owner mismatch did not exit 3: %s" % _ps_detail(signed))
        if "SIGNED" in (signed.stdout or ""):
            self.fail("signer printed SIGNED for a router/owner mismatch")

    def test_signer_refuses_unknown_router(self):
        shell = _ps_shell()
        if not shell:
            self.skipTest("signer unknown-router test: PowerShell not on PATH")
        raw = self.home / "unknown-lead.key"
        raw.write_bytes(bytes(self.keys["Lead"]))
        job_file = self.home / "unknown-router.txt"
        job_file.write_text(
            "id: bad-router\nv: 2\nfrom: Lead\nto: Build\nlane: lab\npriority: normal\n"
            "time: 2026-10-10T12:00:00-04:00\n"
            "routed: Other 2026-10-10T12:00:00-04:00\n"
            "job: list lab pane files\ndone-when: the pane lists its files\npane: 1\n",
            encoding="utf-8",
        )
        signed = self._run_sign(shell, job_file, "bad-router", raw)
        if signed.returncode != 3:
            self.fail("unknown router name did not exit 3: %s" % _ps_detail(signed))
        if "SIGNED" in (signed.stdout or ""):
            self.fail("signer printed SIGNED for an unknown router name")
        if "Grok Bot or Lead" not in (signed.stdout or ""):
            self.fail("signer did not name the allowed routers: %s" % _ps_detail(signed))

    def test_signer_keeps_author_router_name(self):
        shell = _ps_shell()
        if not shell:
            self.skipTest("signer router-name test: PowerShell not on PATH")
        raw = self.home / "router-lead.key"
        raw.write_bytes(bytes(self.keys["Lead"]))
        job_file = self.home / "router-keep.txt"
        job_file.write_text(
            "id: keep-router\nv: 2\nfrom: Lead\nto: Build\nlane: lab\npriority: normal\n"
            "time: 2026-10-10T12:00:00-04:00\n"
            "routed: Lead 2026-10-10T12:00:00-04:00\n"
            "job: list lab pane files\ndone-when: the pane lists its files\npane: 1\n",
            encoding="utf-8",
        )
        signed = self._run_sign(shell, job_file, "keep-router", raw)
        if signed.returncode != 0 or "SIGNED" not in (signed.stdout or ""):
            self.fail("signer did not keep a written router name: %s" % _ps_detail(signed))
        text = job_file.read_text(encoding="utf-8")
        if "routed: Lead " not in text:
            self.fail("author router name was overwritten")

    def test_signer_eastern_offset_check(self):
        script = (ROOT / "tools" / "Sign-BusJob.ps1").read_text(encoding="utf-8")
        iso_fn = _extract_ps_function(script, "Test-HubIso")
        if "-04:00" not in iso_fn or "-05:00" not in iso_fn:
            self.fail("Test-HubIso does not require Eastern offsets")
        shell = _ps_shell()
        if not shell:
            self.skipTest("signer Eastern-offset check: powershell.exe (5.1) or pwsh not on PATH")
        cases = (
            ("2026-10-10T12:00:00-04:00", True),
            ("2026-10-10T12:00:00-05:00", True),
            ("2026-10-10T12:00:00+00:00", False),
            ("2026-10-10T12:00:00-07:00", False),
            ("2026-10-10T12:00:00Z", False),
            ("2026-10-10T12:00:00", False),
        )
        body = (
            "$ErrorActionPreference = \"Stop\"\n"
            f"{iso_fn}\n"
            "foreach ($line in [System.IO.File]::ReadAllLines($args[0])) {\n"
            "    $ok = Test-HubIso ([string]$line)\n"
            "    if ($ok) { Write-Output ('OK ' + $line) } else { Write-Output ('NO ' + $line) }\n"
            "}\n"
        )
        helper = self.home / "hub-iso.ps1"
        payload = self.home / "hub-iso.txt"
        helper.write_text(body, encoding="utf-8")
        payload.write_text("\n".join(value for value, _ok in cases) + "\n", encoding="utf-8")
        completed = _run_ps(shell, helper, (str(payload),))
        if completed.returncode != 0:
            self.fail("Test-HubIso helper failed: %s" % _ps_detail(completed))
        lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
        for value, allowed in cases:
            prefix = "OK " if allowed else "NO "
            if prefix + value not in lines:
                self.fail("Test-HubIso %s for %s: %s" % (
                    "did not accept" if allowed else "did not refuse",
                    value,
                    _ps_detail(completed),
                ))
        raw = self.home / "east-lead.key"
        raw.write_bytes(bytes(self.keys["Lead"]))
        for job_id, stamp in (
            ("east-utc", "2026-10-10T12:00:00+00:00"),
            ("east-pt", "2026-10-10T12:00:00-07:00"),
        ):
            job_file = self.home / ("%s.txt" % job_id)
            job_file.write_text(
                "id: %s\nv: 2\nfrom: Lead\nto: Build\nlane: lab\npriority: normal\n"
                "time: 2026-10-10T12:00:00-04:00\n"
                "routed: Lead %s\n"
                "job: list lab pane files\ndone-when: the pane lists its files\npane: 1\n"
                % (job_id, stamp),
                encoding="utf-8",
            )
            signed = self._run_sign(shell, job_file, job_id, raw)
            if signed.returncode != 3:
                self.fail("%s did not exit 3: %s" % (job_id, _ps_detail(signed)))
            if "SIGNED" in (signed.stdout or ""):
                self.fail("signer printed SIGNED for %s" % job_id)
        ok_file = self.home / "east-ok.txt"
        ok_file.write_text(
            "id: east-ok\nv: 2\nfrom: Lead\nto: Build\nlane: lab\npriority: normal\n"
            "time: 2026-10-10T12:00:00-04:00\n"
            "routed: Lead 2026-10-10T12:00:00-05:00\n"
            "job: list lab pane files\ndone-when: the pane lists its files\npane: 1\n",
            encoding="utf-8",
        )
        signed_ok = self._run_sign(shell, ok_file, "east-ok", raw)
        if signed_ok.returncode != 0 or "SIGNED" not in (signed_ok.stdout or ""):
            self.fail("Eastern -05:00 did not sign: %s" % _ps_detail(signed_ok))

    def test_signer_refuses_empty_bare_datetime_and_gb(self):
        shell = _ps_shell()
        if not shell:
            self.skipTest("signer empty/bare/GB refuse: powershell.exe (5.1) or pwsh not on PATH")
        raw = self.home / "strict-lead.key"
        raw.write_bytes(bytes(self.keys["Lead"]))
        cases = (
            ("empty-r", "routed: \n"),
            ("bare-lead", "routed: Lead\n"),
            ("bare-gb-name", "routed: Grok Bot\n"),
            ("dt-only", "routed: 2026-10-10T12:00:00-04:00\n"),
            ("gb-comma", "routed: GB, 2026-10-10T12:00:00-04:00\n"),
        )
        for job_id, routed_line in cases:
            job_file = self.home / ("%s.txt" % job_id)
            job_file.write_text(
                "id: %s\nv: 2\nfrom: Lead\nto: Build\nlane: lab\npriority: normal\n"
                "time: 2026-10-10T12:00:00-04:00\n%s"
                "job: list lab pane files\ndone-when: the pane lists its files\npane: 1\n"
                % (job_id, routed_line),
                encoding="utf-8",
            )
            signed = self._run_sign(shell, job_file, job_id, raw)
            if signed.returncode != 3:
                self.fail("%s did not exit 3: %s" % (job_id, _ps_detail(signed)))
            if "SIGNED" in (signed.stdout or ""):
                self.fail("signer printed SIGNED for %s" % job_id)

    def test_signer_refuses_whitespace_padded_pane(self):
        shell = _ps_shell()
        if not shell:
            self.skipTest("signer padded-pane refuse: powershell.exe (5.1) or pwsh not on PATH")
        raw = self.home / "pane-ws-lead.key"
        raw.write_bytes(bytes(self.keys["Lead"]))
        cases = (
            ("ws-lead-pane", "pane:  1\n"),
            ("ws-trail-pane", "pane: 2 \n"),
        )
        for job_id, pane_line in cases:
            job_file = self.home / ("%s.txt" % job_id)
            job_file.write_text(
                "id: %s\nv: 2\nfrom: Lead\nto: Build\nlane: lab\npriority: normal\n"
                "time: 2026-10-10T12:00:00-04:00\n"
                "routed: Lead 2026-10-10T12:00:00-04:00\n"
                "job: list lab pane files\ndone-when: the pane lists its files\n%s"
                % (job_id, pane_line),
                encoding="utf-8",
            )
            signed = self._run_sign(shell, job_file, job_id, raw)
            if signed.returncode != 3:
                self.fail("%s did not exit 3: %s" % (job_id, _ps_detail(signed)))
            if "SIGNED" in (signed.stdout or ""):
                self.fail("signer printed SIGNED for %s" % job_id)
            if "BAD_PANE" not in (signed.stdout or ""):
                self.fail("signer did not print BAD_PANE: %s" % _ps_detail(signed))
            text = job_file.read_text(encoding="utf-8")
            if "sig:" in text:
                self.fail("signer wrote a sig for a padded pane")

    def test_signer_p4c_hub_codes(self):
        shell = _ps_shell()
        if not shell:
            self.skipTest("signer P4c codes: powershell.exe (5.1) or pwsh not on PATH")
        raw = self.home / "p4c-lead.key"
        raw.write_bytes(bytes(self.keys["Lead"]))
        header = (
            "v: 2\nfrom: Lead\nto: Build\nlane: lab\npriority: normal\n"
            "time: 2026-10-10T12:00:00-04:00\n"
            "routed: Lead 2026-10-10T12:00:00-04:00\n"
        )
        cases = (
            ("p4c-miss", header + "job: list lab pane files\ndone-when: \npane: 1\n", "MISSING_FIELD"),
            ("p4c-pri", header.replace("priority: normal", "priority: high")
             + "job: list lab pane files\ndone-when: the pane lists its files\npane: 1\n", "BAD_PRIORITY"),
            ("p4c-pane", header + "job: list lab pane files\ndone-when: the pane lists its files\npane: 3\n", "BAD_PANE"),
            ("p4c-verb", header + "job: please send the lab list\ndone-when: the pane lists its files\npane: 1\n", "BAD_VERB"),
            ("p4c-from", header.replace("from: Lead", "from: Grok Bot")
             + "job: list lab pane files\ndone-when: the pane lists its files\npane: 1\n", "OWNER_MISMATCH"),
            ("p4c-span",
             "id: p4c-span\nv: 2\nfrom: Lead\nto: Build\nlane: lab\n"
             "priority: normal\n extra\n"
             "time: 2026-10-10T12:00:00-04:00\n"
             "routed: Lead 2026-10-10T12:00:00-04:00\n"
             "job: list lab pane files\ndone-when: the pane lists its files\npane: 1\n",
             "BAD_FIELD"),
        )
        for job_id, body, code in cases:
            if not body.startswith("id: "):
                body = "id: %s\n%s" % (job_id, body)
            job_file = self.home / ("%s.txt" % job_id)
            job_file.write_text(body, encoding="utf-8")
            signed = self._run_sign(shell, job_file, job_id, raw)
            if signed.returncode != 3:
                self.fail("%s did not exit 3: %s" % (job_id, _ps_detail(signed)))
            if code not in (signed.stdout or ""):
                self.fail("%s did not print %s: %s" % (job_id, code, _ps_detail(signed)))
            if "SIGNED" in (signed.stdout or ""):
                self.fail("signer printed SIGNED for %s" % job_id)

    def test_10_no_secrets_in_logs(self):
        signed = self._sign(self._job(id="scan-1"))
        unsigned = self._job(id="scan-2")
        self._write(signed)
        server.apply_decision("scan-1", "approve")
        self._write(unsigned)
        server.apply_decision("scan-2", "approve")
        text = server.LOG.read_text(encoding="utf-8") if server.LOG.is_file() else ""
        leaked = any(item and item in text for item in self.secrets)
        if leaked:
            self.fail("secret material found in approvals log")
        output = _OUTPUT.getvalue()
        if any(item and item in output for item in self.secrets):
            self.fail("secret material found in test output")
        if "list lab pane files" in output:
            self.fail("job body found in test output")
        for row in self._log_rows():
            if "sig" in row or "key" in row or "job" in row or "body" in row:
                self.fail("approvals log has a forbidden field")
            if "list lab pane files" in json.dumps(row):
                self.fail("approvals log has a job body")


if __name__ == "__main__":
    unittest.main(verbosity=2)
