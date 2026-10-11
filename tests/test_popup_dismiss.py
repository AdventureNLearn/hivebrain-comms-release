"""WebView2 context menus dismiss without becoming TopMost orphans."""

import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _shell_cs():
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted((ROOT / "shell").glob("*.cs")))


def _method_body(source, signature):
    start = source.find(signature)
    if start < 0:
        return ""
    brace = source.find("{", start)
    if brace < 0:
        return ""
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    return source[start:]


def _dotnet():
    env_root = os.environ.get("DOTNET_ROOT")
    candidates = [
        shutil.which("dotnet"),
        str(Path(env_root) / "dotnet") if env_root else None,
        str(Path.home() / ".dotnet" / "dotnet"),
        "/usr/share/dotnet/dotnet",
    ]
    for path in candidates:
        if path and Path(path).is_file():
            return path
    return None


class TestPopupDismiss(unittest.TestCase):
    def test_eleven_dismiss_mutants(self):
        dotnet = _dotnet()
        if not dotnet:
            raise unittest.SkipTest("dotnet SDK is missing; popup dismiss mutants need it")
        project = ROOT / "tests" / "popup_dismiss_check" / "popup_dismiss_check.csproj"
        result = subprocess.run(
            [dotnet, "run", "--project", str(project), "-nologo", "-v", "q"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = (result.stdout or "") + (result.stderr or "")
        stdout = result.stdout or ""
        if result.returncode != 0 or "11 ok" not in stdout or "controller ok" not in stdout or "nested-click ok" not in stdout:
            self.fail("popup dismiss mutants failed: %s" % out)

    def test_webview2_menu_is_owned_and_deferral_completes_on_item_click(self):
        text = _shell_cs()
        if (ROOT / "page-menus.js").exists():
            self.fail("page-menus.js is unused and should be gone")
        if "AddScriptToExecuteOnDocumentCreated" in text:
            self.fail("dismiss script is injected on every origin")
        if "ShouldAttachHubDismiss" not in text:
            self.fail("hub-origin gate is missing")
        if 'Host != "127.0.0.1"' not in text or "UserInfo" not in text:
            self.fail("hub origin is not the #12 exact check")
        if "IsLoopback" in text:
            self.fail("hub origin still uses IsLoopback")
        if "OwnedMenuController" not in text:
            self.fail("context menu is not driven by OwnedMenuController")
        if "OnItemClicked" not in text:
            self.fail("ItemClicked does not go through the controller")
        if "entry.Click" in text:
            self.fail("SelectedCommandId is still set on Click")
        if "ItemClicked" not in text:
            self.fail("context-menu deferral is not completed on ItemClicked")
        if "class NativeMethods" not in text or "GetForegroundWindow" not in text:
            self.fail("GetForegroundWindow is not in NativeMethods")
        host = (ROOT / "shell" / "PopupHost.cs").read_text(encoding="utf-8")
        if "DllImport" in host:
            self.fail("PopupHost still declares its own DllImport")
        if "new ContextMenuStrip" in text:
            self.fail("ContextMenuStrip is created outside the owned-menu helper")
        if "Show(view, e.Location)" not in host:
            self.fail("WebView2 menu is not shown against the view")
        if "Owner = this" not in text or "Show(this)" not in text:
            self.fail("child popup forms are not owned by the main form")
        if "ShouldBindChildFormEscape" not in text:
            self.fail("child-form Esc is not gated by the helper")
        if "HookOwnedMenuClicks" not in host or "entry.DropDown" not in host:
            self.fail("submenu ItemClicked is not hooked recursively")
        if "ScheduleDispose" not in text or "BeginInvoke" not in host:
            self.fail("menu dispose is not scheduled on BeginInvoke")
        if "SkipTest" not in (ROOT / "tests" / "test_popup_dismiss.py").read_text(encoding="utf-8"):
            self.fail("test_eleven_dismiss_mutants no longer skips without dotnet")

    def test_nested_dropdown_itemclicked_call_is_recursive(self):
        host = (ROOT / "shell" / "PopupHost.cs").read_text(encoding="utf-8")
        hook = _method_body(host, "private void HookOwnedMenuClicks(")
        foreach_hdr = re.search(r"foreach\s*\(\s*ToolStripItem\s+item\s+in\s+drop\.Items\s*\)", hook)
        if not foreach_hdr:
            self.fail("HookOwnedMenuClicks has no foreach over drop.Items")
        foreach_block = _method_body(hook[foreach_hdr.start():], "foreach")
        brace = foreach_block.find("{")
        body = foreach_block[brace + 1 : foreach_block.rfind("}")].strip()
        if not re.fullmatch(
            r"if\s*\(\s*item\s+is\s+not\s+ToolStripMenuItem\s+entry"
            r"\s*\|\|\s*entry\.DropDownItems\.Count\s*==\s*0\s*\)\s*continue\s*;"
            r"\s*HookOwnedMenuClicks\s*\(\s*entry\.DropDown\s*,\s*session\s*\)\s*;",
            body,
            re.S,
        ):
            self.fail("nested DropDown ItemClicked is not hooked unconditionally")

    def test_schedule_dispose_uses_begin_invoke_not_direct_dispose(self):
        host = (ROOT / "shell" / "PopupHost.cs").read_text(encoding="utf-8")
        method = _method_body(host, "public void ScheduleDispose()")
        local = _method_body(method, "void DisposeNow()")
        outer = method.replace(local, "")
        if "marshal.BeginInvoke(" not in outer:
            self.fail("ScheduleDispose no longer marshals dispose with marshal.BeginInvoke(")
        if "DisposeNow" not in method:
            self.fail("ScheduleDispose lost the DisposeNow fallback")
        if "IsHandleCreated" not in method or "!marshal.Disposing" not in method:
            self.fail("BeginInvoke is not guarded with IsHandleCreated && !Disposing")
        if "catch (InvalidOperationException)" not in method:
            self.fail("BeginInvoke is not wrapped in try/catch with DisposeNow fallback")
        if "menu.Dispose()" not in local:
            self.fail("DisposeNow does not dispose the menu")
        if "menu.Dispose()" in outer:
            self.fail("ScheduleDispose disposes the menu directly instead of BeginInvoke")


if __name__ == "__main__":
    unittest.main()
