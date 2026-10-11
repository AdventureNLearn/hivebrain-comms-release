"""PEB ReadProcessMemory buffers cover the bytes they write."""

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "shell"

from tests.runtime_skip import skip_if_dotnet_unrunnable, skip_if_no_dotnet, dotnet_path


def _shell_cs():
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(SHELL.glob("*.cs")))


_ALLOC = re.compile(r"AllocHGlobal\(([^)]+)\)")
_RPM = re.compile(
    r"ReadProcessMemory\s*\(\s*[^,]+,\s*[^,]+,\s*[^,]+,\s*([^,]+),\s*(out\s+[^)]+)\)"
)


class TestPebRead(unittest.TestCase):
    def test_buffer_size_mutants(self):
        skip_if_no_dotnet(self)
        project = ROOT / "tests" / "peb_read_check" / "peb_read_check.csproj"
        result = subprocess.run(
            [dotnet_path(), "run", "--project", str(project), "-nologo", "-v", "q"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = (result.stdout or "") + (result.stderr or "")
        skip_if_dotnet_unrunnable(self, result.returncode, out, label="peb read mutants")
        if result.returncode != 0 or "9 ok" not in (result.stdout or ""):
            self.fail("peb read mutants failed: %s" % out)

    def test_alloc_covers_every_readprocessmemory(self):
        text = _shell_cs()
        if re.search(r"AllocHGlobal\(\s*IntPtr\.Size\s*\)", text):
            self.fail("AllocHGlobal(IntPtr.Size) is the old 8-byte x64 smash")
        if re.search(r"IntPtr\.Size\s*\*\s*2", text):
            self.fail("ReadProcessMemory size IntPtr.Size * 2 overflowed the pointer buffer")
        alloc_args = [m.group(1).strip() for m in _ALLOC.finditer(text)]
        if not alloc_args:
            self.fail("no AllocHGlobal in shell/")
        for arg in alloc_args:
            if "RequiredAllocSize" not in arg and arg != "allocSize":
                self.fail("AllocHGlobal(%s) is not RequiredAllocSize" % arg)
        if "RequiredAllocSize" not in text:
            self.fail("RequiredAllocSize is not used")
        for match in _RPM.finditer(text):
            size = match.group(1).strip()
            read = match.group(2).strip()
            if read == "out _":
                self.fail("ReadProcessMemory ignores bytesRead")
            if size in ("IntPtr.Size", "IntPtr.Size * 2"):
                self.fail("ReadProcessMemory size %s is not bounded" % size)

    def test_tryread_uses_bytes_read_enough_and_frees_hglobal(self):
        peb = (SHELL / "PebRead.cs").read_text(encoding="utf-8")
        if "BytesReadEnough(read.ToInt64(), size)" not in peb:
            self.fail("TryRead dropped BytesReadEnough")
        try_read = peb.split("private static bool TryRead", 1)[1].split("[DllImport", 1)[0]
        if "BytesReadEnough" not in try_read:
            self.fail("TryRead does not call BytesReadEnough")
        if "Marshal.FreeHGlobal(block)" not in peb:
            self.fail("PebRead dropped FreeHGlobal")
        if "finally { Marshal.FreeHGlobal(block); }" not in peb:
            self.fail("FreeHGlobal is not in a finally")
        if peb.count("AllocHGlobal") < 1:
            self.fail("AllocHGlobal is gone")
        if peb.count("FreeHGlobal") < 1:
            self.fail("FreeHGlobal is gone")

    def test_openprocess_peb_setlasterror_and_process_dispose(self):
        text = _shell_cs()
        peb = (SHELL / "PebRead.cs").read_text(encoding="utf-8")
        if 'DllImport("kernel32.dll", SetLastError = true)]' not in peb:
            self.fail("OpenProcess/ReadProcessMemory missing SetLastError")
        if "SetLastError = true" not in peb or "OpenProcess" not in peb:
            self.fail("PEB P/Invokes missing SetLastError")
        if peb.count("SetLastError = true") < 4:
            self.fail("not every PEB P/Invoke sets SetLastError")
        if "GetProcessById" in text and "using (proc)" not in text:
            self.fail("GetProcessById result is not disposed")
        named = [m.start() for m in re.finditer(r"GetProcessesByName\(", text)]
        if not named:
            self.fail("GetProcessesByName is gone")
        for index in named:
            window = text[index : index + 280]
            if "using (proc)" not in window:
                self.fail("GetProcessesByName result is not disposed")


if __name__ == "__main__":
    unittest.main()
