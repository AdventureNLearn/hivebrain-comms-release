"""StopHub and the staging launcher only touch processes we started."""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

import server


def _ps_shell():
    """Windows PowerShell 5.1 first; pwsh 7 is optional."""
    return shutil.which("powershell") or shutil.which("pwsh")


def _extract_ps_function(text, name):
    token = "function %s" % name
    start = text.find(token)
    if start < 0:
        raise AssertionError("launcher is missing %s" % name)
    brace = text.find("{", start)
    depth = 0
    for index in range(brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise AssertionError("launcher function %s is incomplete" % name)


SKIP_DIRS = {
    ".git",
    "shell/bin",
    "shell/obj",
    "shell/staging",
    "__pycache__",
    "node_modules",
}
SCRIPT_SUFFIXES = (".ps1", ".cmd", ".bat", ".cs", ".py")
PIPELINE_KILL = re.compile(
    r"Get-Process[\s\S]{0,200}\|\s*Stop-Process",
    re.IGNORECASE,
)
SPPS = re.compile(r"(?<![A-Za-z])spps(?![A-Za-z])", re.IGNORECASE)
BARE_KILL = re.compile(r"(?<!\.)\bkill\b", re.IGNORECASE)
TASKKILL_IM = re.compile(r"taskkill[\s\S]{0,80}/IM", re.IGNORECASE)
TASKKILL_FI = re.compile(r"taskkill[\s\S]{0,120}/FI[\s\S]{0,80}IMAGENAME", re.IGNORECASE)
DOT_KILL = re.compile(r"\.Kill\s*\(")
STOP_PROCESS = re.compile(r"\bStop-Process\b", re.IGNORECASE)
COMPUTED_METHOD = re.compile(r"\$[A-Za-z_][\w]*\s*\.\s*\$[A-Za-z_][\w]*\s*\(")
CIM_TERM = re.compile(r"Invoke-CimMethod[\s\S]{0,80}Terminate", re.IGNORECASE)
WMIC_DEL = re.compile(r"wmic[\s\S]{0,160}\bdelete\b", re.IGNORECASE)
TERM_PROC = re.compile(r"TerminateProcess", re.IGNORECASE)
JOINED = re.compile(
    r"(['\"][\w\-/.]{0,24}['\"]\s*\+\s*)+['\"][\w\-/.]{0,24}['\"]",
    re.IGNORECASE,
)
JOIN_OP = re.compile(r"-join[\s\S]{0,80}(Stop|kill|taskkill|Terminate)", re.IGNORECASE)
AMP_JOIN = re.compile(r"&\s*\(\s*\$[^)]+\+[^)]+\)")
AMP_EXPAND = re.compile(r"&\s*[\"'][^\"']*\$[{A-Za-z_]")
GETATTR_OS = re.compile(r"getattr\s*\(\s*os\s*,[\s\S]{0,80}\.join\s*\(")
PSUTIL_KILL = re.compile(r"psutil[\s\S]{0,160}\.(?:kill|terminate)\s*\(", re.IGNORECASE)
OS_KILL = re.compile(r"(?<![A-Za-z_])os\.kill\s*\(")
PY_DOT_KILL = re.compile(r"(?<!os)\.(?:kill|terminate)\s*\(", re.IGNORECASE)
ALLOW_MARK = "hbc-allow-kill"
TASKKILL_TREE_LINE = '["taskkill", "/T", "/F", "/PID"'
PYTHON_IMAGE_REGEX = r"^pythonw?(?:\d+(?:\.\d+)*)?\.exe$"


def _iter_script_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SCRIPT_SUFFIXES:
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel == "tests/test_shell_process_guard.py":
            continue
        if any(rel == skip or rel.startswith(skip + "/") for skip in SKIP_DIRS):
            continue
        yield path


def _stop_hub_span(text):
    start = text.find("private void StopHub()")
    if start < 0:
        return -1, -1
    end = text.find("private sealed class StartedProcess", start)
    if end < 0:
        end = text.find("\n    private ", start + 1)
    if end < 0:
        end = len(text)
    return start, end


def _line_of(text, index):
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    if end < 0:
        end = len(text)
    return text[start:end]


def _line_allowed(rel, text, index):
    line = _line_of(text, index)
    if ALLOW_MARK in line:
        return True
    if rel.replace("\\", "/") == "hub/status.py" and TASKKILL_TREE_LINE in line:
        return True
    return False


def _tracked_hub_kill(text, index):
    start, end = _stop_hub_span(text)
    if start < 0 or not (start <= index < end):
        return False
    body = text[start:end]
    if body.count(".Kill(") != 1:
        return False
    return (
        "HubIdentityMatches" in body
        and "ReadStartedProcess" in body
        and "started.Pid" in body
    )


def _joined_alias(text):
    if JOIN_OP.search(text):
        return True
    for match in JOINED.finditer(text):
        blob = re.sub(r"['\"\s\+]", "", match.group(0)).lower()
        if blob in (
            "stop-process",
            "stopprocess",
            "kill",
            "taskkill",
            "spps",
            "terminateprocess",
        ):
            return True
    return False


def kill_hits(text, label, rel=""):
    hits = []
    rel = rel or label

    def _ok(index=0):
        return _line_allowed(rel, text, index)

    def _flag(regex, label_text, skip_py_english=False):
        for match in regex.finditer(text):
            if _ok(match.start()):
                continue
            if skip_py_english and rel.endswith(".py"):
                continue
            hits.append("%s: %s" % (label, label_text))
            return

    _flag(PIPELINE_KILL, "Get-Process|Stop-Process")
    _flag(SPPS, "spps")
    _flag(BARE_KILL, "kill", skip_py_english=True)
    _flag(TASKKILL_IM, "taskkill /IM")
    _flag(TASKKILL_FI, "taskkill /FI IMAGENAME")
    if STOP_PROCESS.search(text) and not PIPELINE_KILL.search(text):
        match = STOP_PROCESS.search(text)
        if match and not _ok(match.start()):
            hits.append("%s: Stop-Process" % label)
    _flag(COMPUTED_METHOD, "computed $p.$m()")
    _flag(CIM_TERM, "Invoke-CimMethod Terminate")
    _flag(WMIC_DEL, "wmic delete")
    _flag(TERM_PROC, "TerminateProcess")
    _flag(AMP_JOIN, "& ($a+$b)")
    _flag(AMP_EXPAND, '& "Stop-$x"')
    _flag(GETATTR_OS, "getattr(os, ...)")
    _flag(PSUTIL_KILL, "psutil .kill/.terminate")
    for match in OS_KILL.finditer(text):
        if _ok(match.start()):
            continue
        if re.search(r",\s*0\s*\)", _line_of(text, match.start())):
            continue
        hits.append("%s: os.kill" % label)
        break
    if _joined_alias(text):
        match = JOIN_OP.search(text) or JOINED.search(text)
        if match and not _ok(match.start()):
            hits.append("%s: joined-string alias" % label)
    for match in DOT_KILL.finditer(text):
        if _ok(match.start()) or _tracked_hub_kill(text, match.start()):
            continue
        hits.append("%s: .Kill() outside the tracked hub process" % label)
    if rel.endswith(".py"):
        for match in PY_DOT_KILL.finditer(text):
            if _ok(match.start()) or _tracked_hub_kill(text, match.start()):
                continue
            hits.append("%s: .kill/.terminate in Python" % label)
            break
    return hits


def _stop_hub_body():
    text = (ROOT / "shell" / "Form1.cs").read_text(encoding="utf-8")
    start, end = _stop_hub_span(text)
    if start < 0:
        raise AssertionError("StopHub is missing")
    return text[start:end]


class TestShellProcessGuard(unittest.TestCase):
    def test_stophub_uses_exact_identity(self):
        body = _stop_hub_body()
        form = (ROOT / "shell" / "Form1.cs").read_text(encoding="utf-8")
        if 'GetProcessesByName("python")' in body or "GetProcessesByName" in body:
            self.fail("StopHub still scans by process name")
        if "ListeningOnHub" in body:
            self.fail("StopHub still scans listeners by name")
        if "HubIdentityMatches" not in body:
            self.fail("StopHub does not use the exact identity check")
        if PYTHON_IMAGE_REGEX not in form:
            self.fail("identity check does not use the Store-python image regex")
        if "python3.12.exe" not in form and "Microsoft Store" not in form:
            self.fail("Store python image names are not documented")
        if 'Equals("server.py", StringComparison.Ordinal)' not in form:
            self.fail("identity check does not require argv[1] to be exactly server.py")
        if "ProcessCwd" not in form:
            self.fail("identity check does not read the process cwd")
        if "ToFileTimeUtc" not in form:
            self.fail("identity check does not match create time")
        if body.count(".Kill(") != 1:
            self.fail("StopHub must contain exactly one .Kill(")

    def test_stophub_refuses_missing_corrupt_stale(self):
        body = _stop_hub_body()
        for message in (
            "Hub process record is missing. Not stopping.",
            "Hub process record is corrupt. Not stopping.",
            "Hub process record is stale. Not stopping.",
            "Hub process record could not be read. Not stopping.",
        ):
            if message not in body:
                self.fail("StopHub missing refuse message: %s" % message)
        if "ReadStartedProcess" not in body:
            self.fail("ReadStartedProcess is not inside StopHub")
        if body.count("Say(") < 4:
            self.fail("StopHub does not show a message when the record is unusable")

    def test_listening_on_hub_netstat_is_gone(self):
        hits = []
        for path in _iter_script_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            rel = path.relative_to(ROOT).as_posix()
            if "ListeningOnHub" in text:
                hits.append("%s: ListeningOnHub" % rel)
            if "netstat.exe" in text:
                hits.append("%s: netstat.exe" % rel)
        if hits:
            self.fail("dead listener scan still present: %s" % "; ".join(hits))

    def test_hub_writes_its_own_process_record(self):
        text = (ROOT / "server.py").read_text(encoding="utf-8")
        starter = (ROOT / "Start-HiveBrainComms.ps1").read_text(encoding="utf-8")
        if "def write_hub_process" not in text or "def clear_hub_process" not in text:
            self.fail("hub does not own the .hub-process record")
        if "def start_hub" not in text:
            self.fail("start_hub is missing")
        start_hub = text.split("def start_hub", 1)[1].split("\ndef ", 1)[0]
        if "write_hub_process()" not in start_hub:
            self.fail("start_hub does not write .hub-process")
        if "clear_hub_process()" not in text:
            self.fail("clean exit does not remove .hub-process")
        if ".hub-process" in starter:
            self.fail("the launcher still writes .hub-process; the hub must write it")
        before, _ = text.split("def write_hub_process", 1)
        if "write_hub_process()" in before:
            self.fail("importing server writes .hub-process")

    def test_live_guard_refuses_hub_process(self):
        from tests.live_guard import install_live_state_guard, live_hub_paths

        install_live_state_guard()
        live_hub = live_hub_paths()[-1]
        before = live_hub.read_bytes() if live_hub.is_file() else None
        try:
            server.write_hub_process()
        except AssertionError as exc:
            if ".hub-process" not in str(exc):
                self.fail("live guard did not mention .hub-process")
        else:
            self.fail("write_hub_process wrote the live record")
        after = live_hub.read_bytes() if live_hub.is_file() else None
        if after != before:
            self.fail("live .hub-process changed")

    def test_write_hub_process_is_atomic_and_cleared(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            path = tmp / ".hub-process"
            with mock.patch.object(server, "ROOT", tmp), mock.patch.object(
                server, "HUB_PROCESS", path
            ):
                server.write_hub_process()
                rec = path.read_text(encoding="utf-8")
                if "pid=%s" % __import__("os").getpid() not in rec:
                    self.fail("hub process record missing pid")
                if "ctime=" not in rec:
                    self.fail("hub process record missing ctime")
                server.clear_hub_process()
                if path.exists():
                    self.fail("clean exit left .hub-process")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_launcher_does_not_overwrite_by_title(self):
        text = (ROOT / "Focus-Or-Start-Shell.ps1").read_text(encoding="utf-8")
        if "MainWindowTitle" in text:
            self.fail("launcher still uses the window title")
        if "GetProcessesByName" in text:
            self.fail("launcher still scans by process name")
        if "Get-SwapBlockers" not in text or "MainModule" not in text:
            self.fail("launcher does not inspect Shell.exe/dotnet MainModule or path")
        if "command line unreadable" not in text:
            self.fail("launcher does not fail closed when a command line cannot be read")
        if "Invoke-AtomicLiveSwap" not in text or "live-incoming-" not in text:
            self.fail("launcher does not stage into a sibling folder")
        if "Clear-StaleIncoming" not in text:
            self.fail("launcher does not sweep stale live-incoming-* folders")
        if "ReparsePoint" not in text:
            self.fail("incoming sweep does not skip reparse points")
        if "RESTORE FAILED. Live folder is missing. Backup is still at" not in text:
            self.fail("launcher does not log the backup path when restore fails")
        if 'if ("$_" -notmatch "RESTORE FAILED")' not in text and "-notmatch \"RESTORE FAILED\"" not in text:
            self.fail("launcher still says the live folder was unchanged after RESTORE FAILED")
        if "Swap succeeded. Cleanup warning:" not in text:
            self.fail("launcher does not warn when cleanup fails after a good swap")
        if "Rename-Item" not in text:
            self.fail("launcher does not rename the staged folder into place")
        if re.search(r"Copy-Item[\s\S]{0,120}-Destination \$live\b", text):
            self.fail("launcher still copies straight into the live folder")
        if "Atomic stage-and-swap failed. Live folder was left unchanged." not in text:
            self.fail("launcher does not refuse a failed atomic swap")
        if "A Shell.exe or dotnet process is using the live folder. Not overwriting." not in text:
            self.fail("launcher does not refuse overwrite when a live Shell path is running")
        if "A Shell.exe or dotnet command line could not be read. Not overwriting." not in text:
            self.fail("launcher does not refuse overwrite when a command line is unreadable")
        if "App process record is missing. Not overwriting." not in text:
            self.fail("launcher does not refuse overwrite when the record is missing")
        if "ToFileTimeUtc" not in text:
            self.fail("launcher does not use create time")
        if "Write-StartedProcess" not in text or ".shell-process" not in text:
            self.fail("launcher does not record the app it started")
        if "Not starting another." not in text:
            self.fail("launcher does not stop on a corrupt or stale record")
        if "Resolve-AppProcessRecordPath" not in text:
            self.fail("launcher does not resolve the app-process record from the root")
        if "GetFullPath" not in text:
            self.fail("launcher does not build an absolute record path")
        if re.search(r"File\]::Replace\([^)]*\$null", text):
            self.fail("File.Replace still uses a null backup path")
        if "Stop will refuse" not in text:
            self.fail("launcher does not warn when the app-process record cannot be written")

    def test_write_started_process_replaces_existing_after_swap(self):
        shell = _ps_shell()
        if not shell:
            self.skipTest(
                "Write-StartedProcess replace: powershell.exe (5.1) or pwsh not on PATH"
            )
        src = (ROOT / "Focus-Or-Start-Shell.ps1").read_text(encoding="utf-8")
        tmp = Path(tempfile.mkdtemp())
        try:
            script = tmp / "replace-record.ps1"
            body = (
                "param([Parameter(Mandatory = $true)][string]$Root)\n"
                "$ErrorActionPreference = \"Stop\"\n"
                + _extract_ps_function(src, "Write-GuardMessage")
                + "\n"
                + _extract_ps_function(src, "Get-UnquotedPath")
                + "\n"
                + _extract_ps_function(src, "Resolve-AppProcessRecordPath")
                + "\n"
                + _extract_ps_function(src, "Write-StartedProcess")
                + "\n"
                "$cases = @(\n"
                "    '.shell-process',\n"
                "    '\".shell-process\"',\n"
                "    \"'.shell-process'\",\n"
                "    '.\\.shell-process',\n"
                "    '\".\\.shell-process\"',\n"
                "    '  \".shell-process\"  '\n"
                ")\n"
                "foreach ($p in $cases) {\n"
                "    $full = Join-Path $Root '.shell-process'\n"
                "    $utf8 = New-Object System.Text.UTF8Encoding $false\n"
                "    [System.IO.File]::WriteAllText($full, \"pid=1`nctime=1`n\", $utf8)\n"
                "    Write-StartedProcess -ProcId $PID -Path $p -Root $Root\n"
                "    if (-not (Test-Path -LiteralPath $full)) {\n"
                "        throw (\"no record after replace for {0}\" -f $p)\n"
                "    }\n"
                "    $rec = [System.IO.File]::ReadAllText($full, $utf8)\n"
                "    if ($rec -notmatch ('(?m)^pid=' + $PID + '$')) {\n"
                "        throw (\"record pid was not replaced for {0}: {1}\" -f $p, $rec)\n"
                "    }\n"
                "    if ($rec -notmatch '(?m)^ctime=-?\\d+$') {\n"
                "        throw (\"record missing ctime for {0}: {1}\" -f $p, $rec)\n"
                "    }\n"
                "    if ($rec -match '(?m)^pid=1$') {\n"
                "        throw (\"stale pid=1 remains for {0}\" -f $p)\n"
                "    }\n"
                "}\n"
                "Write-Output 'REPLACED'\n"
            )
            script.write_text(body, encoding="utf-8")
            result = subprocess.run(
                [
                    shell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                    "-Root",
                    str(tmp),
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            out = (result.stdout or "") + (result.stderr or "")
            if result.returncode != 0 or "REPLACED" not in (result.stdout or ""):
                self.fail("existing record was not replaced after swap: %s" % out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_kill_guard_globs_scripts_csharp_and_python(self):
        hits = []
        seen = 0
        py_seen = 0
        for path in _iter_script_files():
            seen += 1
            rel = path.relative_to(ROOT).as_posix()
            if path.suffix.lower() == ".py":
                py_seen += 1
            hits.extend(kill_hits(path.read_text(encoding="utf-8", errors="replace"), rel, rel))
        if seen < 8 or py_seen < 3:
            self.fail("kill guard did not glob scripts, C#, and Python: %s py=%s" % (seen, py_seen))
        if hits:
            self.fail("name-kill still present: %s" % "; ".join(hits))

    def test_kill_guard_mutant_get_process_python_stop_process(self):
        text = (ROOT / "Focus-Or-Start-Shell.ps1").read_text(encoding="utf-8")
        mutant = text + "\nGet-Process python | Stop-Process -Force\n"
        hits = kill_hits(mutant, "Focus-Or-Start-Shell.ps1")
        if not any("Get-Process|Stop-Process" in item for item in hits):
            self.fail("kill guard missed Get-Process python | Stop-Process -Force")

    def test_kill_guard_allowlists_only_taskkill_tree_line(self):
        path = ROOT / "hub" / "status.py"
        if not path.is_file():
            self.skipTest("hub/status.py is not on this branch")
        text = path.read_text(encoding="utf-8")
        hits = kill_hits(text, "hub/status.py", "hub/status.py")
        if hits:
            self.fail("exact taskkill tree line still flagged: %s" % "; ".join(hits))
        poisoned = text.replace("def _kill_tree", 'def _kill_tree\n    x = "taskkill /IM python.exe"')
        hits = kill_hits(poisoned, "hub/status.py", "hub/status.py")
        if not any("taskkill /IM" in item for item in hits):
            self.fail("kill guard allow-listed the whole _kill_tree function")
        outside = text + '\ntaskkill /FI "IMAGENAME eq python.exe"\n'
        hits = kill_hits(outside, "hub/status.py", "hub/status.py")
        if not any("taskkill /FI IMAGENAME" in item for item in hits):
            self.fail("kill guard missed taskkill outside the tree line")

    def test_kill_guard_mutants_joined_computed_and_wmi(self):
        cases = (
            ("'Stop'+'-Process'", "joined-string alias"),
            ("$p.$m()", "computed $p.$m()"),
            ('taskkill /FI "IMAGENAME eq python.exe"', "taskkill /FI IMAGENAME"),
            ("Invoke-CimMethod -MethodName Terminate", "Invoke-CimMethod Terminate"),
            ("wmic process where name='python.exe' delete", "wmic delete"),
            ("TerminateProcess(handle, 1)", "TerminateProcess"),
            ("& ($a+$b)", "& ($a+$b)"),
            ('& "Stop-$x"', '& "Stop-$x"'),
            ('getattr(os, "".join(["ki", "ll"]))', "getattr(os, ...)"),
            ("psutil.Process(1).terminate()", "psutil .kill/.terminate"),
            ("os.kill(1, 9)", "os.kill"),
            ("proc.kill()", ".kill/.terminate in Python"),
        )
        base = (ROOT / "Focus-Or-Start-Shell.ps1").read_text(encoding="utf-8")
        for snippet, label in cases:
            rel = "mutant.py" if snippet.startswith(("getattr", "psutil", "os.kill", "proc.")) else "mutant.ps1"
            hits = kill_hits(base + "\n" + snippet + "\n", rel, rel)
            if not any(label in item for item in hits):
                self.fail("kill guard missed %s" % snippet)


if __name__ == "__main__":
    unittest.main()
