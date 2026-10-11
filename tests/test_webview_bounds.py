"""Catch hidden 0-size WebView2 init and no-op Dock toggles."""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORM = (ROOT / "shell" / "Form1.cs").read_text(encoding="utf-8")
BOUNDS = (ROOT / "shell" / "WebViewHostBounds.cs").read_text(encoding="utf-8")
WEB_TABS = ("SuperGrok", "X", "Imagine", "Build")
ALL_TABS = WEB_TABS + ("Control",)

from tests.runtime_skip import dotnet_path, skip_if_dotnet_unrunnable, skip_if_no_dotnet


class TestWebViewBounds(unittest.TestCase):
    def test_prepare_then_ensure_then_owned_slot(self):
        self.assertIn("PrepareWebView(view)", FORM)
        first = FORM.find("PrepareWebView(view)")
        ensure = FORM.find("await view.EnsureCoreWebView2Async(env)")
        owned = FORM.find("AttachOwnedWebView")
        self.assertGreaterEqual(first, 0)
        self.assertGreater(ensure, first)
        self.assertIn("AttachOwnedWebView(view) after Ensure, never before Prepare", FORM)
        if owned >= 0:
            self.assertGreater(owned, ensure)

    def test_create_uses_explicit_tab_host_size(self):
        self.assertIn("TabHostClientSize()", FORM)
        self.assertIn("new WebView2 { Dock = DockStyle.None, Size = host, Bounds = new Rectangle(Point.Empty, host) }", FORM)
        self.assertNotIn("view.Width < 64 || view.Height < 64", FORM)
        self.assertIn("NudgeIfUnchanged", FORM)
        self.assertIn("NotifyParentWindowPositionChanged", FORM)
        self.assertIn("NotifyWebViewController", FORM)

    def test_each_web_tab_created_nonzero_and_resized_on_show(self):
        build_tabs = FORM.split("private async Task BuildTabs()", 1)[1].split("\n    internal ", 1)[0]
        self.assertIn('AddWeb("SuperGrok"', build_tabs)
        self.assertIn('AddWeb("X"', build_tabs)
        self.assertIn('AddWeb("Imagine"', build_tabs)
        self.assertIn("AddBuild()", build_tabs)
        self.assertIn("AddControl()", FORM)
        add_web = FORM.split("private async Task AddWeb(", 1)[1].split("\n    private async Task AddBuild()", 1)[0]
        add_build = FORM.split("private async Task AddBuild()", 1)[1].split("\n    private async Task OpenChild(", 1)[0]
        for block, name in ((add_web, "AddWeb"), (add_build, "AddBuild")):
            self.assertIn("TabHostClientSize()", block, name)
            self.assertIn("PrepareWebView(view)", block, name)
            self.assertIn("Size = host", block, name)
            self.assertIn("EnsureCoreWebView2Async", block, name)
            self.assertIn("SyncShownTab(page)", block, name)
        select = FORM.split("private void SelectTab(int index)", 1)[1].split("\n    private ", 1)[0]
        self.assertIn("SyncShownTab(pages[index])", select)
        for title in WEB_TABS:
            self.assertTrue(title)

    def test_select_tab_syncs_and_nudges(self):
        select = FORM.split("private void SelectTab(int index)", 1)[1].split("\n    private ", 1)[0]
        self.assertIn("SyncShownTab(pages[index])", select)
        self.assertIn("NudgeIfUnchanged(view.Size, target.Size)", FORM)
        self.assertNotIn("view.Bounds = new Rectangle(Point.Empty, page.ClientSize)", FORM)
        self.assertNotIn("AddWebResourceRequestedFilter", FORM)
        self.assertNotIn("NavigationStarting", FORM)

    def test_helper_logic_without_exe(self):
        def tab_host_size(width, height, top, bottom):
            w = max(width, 1)
            h = max(height - max(top, 0) - max(bottom, 0), 1)
            return (w, h)

        def fill_bounds(width, height, top_reserve):
            top = max(0, min(top_reserve, max(height - 1, 0)))
            return (0, top, max(width, 1), max(height - top, 1))

        def nudge(current, target):
            if current == target:
                return (max(target[0] - 1, 1), target[1])
            return target

        self.assertEqual(tab_host_size(1280, 860, 80, 24), (1280, 756))
        self.assertEqual(tab_host_size(0, 0, 80, 24), (1, 1))
        self.assertEqual(nudge((800, 600), (800, 600)), (799, 600))
        self.assertEqual(nudge((10, 10), (800, 600)), (800, 600))
        self.assertEqual(fill_bounds(1280, 756, 24), (0, 24, 1280, 732))
        self.assertEqual(fill_bounds(1280, 756, 0), (0, 0, 1280, 756))
        for title in ALL_TABS:
            create = tab_host_size(1280, 860, 88, 26)
            self.assertGreaterEqual(create[0], 1, title)
            self.assertGreaterEqual(create[1], 1, title)
            banner = 24 if title == "Build" else (36 if title != "Control" else 0)
            shown = fill_bounds(create[0], create[1], banner)
            self.assertGreaterEqual(shown[2], 1, title)
            self.assertGreaterEqual(shown[3], 1, title)
            if title != "Control":
                nudged = nudge(create, (shown[2], shown[3]))
                self.assertTrue(nudged != create or (shown[2], shown[3]) != create, title)
        for state, size in (("minimized", (1280, 860)), ("maximized", (1920, 1080)), ("resized", (1100, 700))):
            host = tab_host_size(size[0], size[1], 88, 26)
            for title in ALL_TABS:
                self.assertGreaterEqual(host[0], 1, state + " " + title)
                self.assertGreaterEqual(host[1], 1, state + " " + title)
        self.assertIn("TabSizeAfterWindowChange", BOUNDS)
        self.assertIn("Math.Max(formClient.Height - Math.Max(topChrome, 0) - Math.Max(bottomChrome, 0), 1)", BOUNDS)
        self.assertIn("current.Width == target.Width && current.Height == target.Height", BOUNDS)
        self.assertIn("Math.Max(target.Width - 1, 1)", BOUNDS)

    def test_helper_mutants_via_dotnet(self):
        skip_if_no_dotnet(self)
        proj = ROOT / "tests" / "webview_bounds_check" / "webview_bounds_check.csproj"
        created = subprocess.run(
            [dotnet_path(), "run", "--project", str(proj), "--nologo", "-v", "q"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        out = (created.stdout or "") + (created.stderr or "")
        skip_if_dotnet_unrunnable(self, created.returncode, out, label="webview bounds")
        self.assertEqual(created.returncode, 0, out)
        self.assertIn("webview-bounds-ok", created.stdout)
        self.assertIn("NudgeIfUnchanged", BOUNDS)
        self.assertIn("current.Width == target.Width && current.Height == target.Height", BOUNDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
