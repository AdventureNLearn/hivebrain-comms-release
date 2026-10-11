"""Design-package mapping onto the real Form1 tabs, states, and popups."""

import re
import subprocess
import unittest
from pathlib import Path

from tests.runtime_skip import dotnet_path, skip_if_dotnet_unrunnable, skip_if_no_dotnet

ROOT = Path(__file__).resolve().parents[1]
FORM = (ROOT / "shell" / "Form1.cs").read_text(encoding="utf-8")
CONTROL = (ROOT / "shell" / "ControlTab.cs").read_text(encoding="utf-8")
POPUP = (ROOT / "shell" / "PopupHost.cs").read_text(encoding="utf-8")
BOUNDS = (ROOT / "shell" / "WebViewHostBounds.cs").read_text(encoding="utf-8")
STATE = (ROOT / "shell" / "TabStatePanel.cs").read_text(encoding="utf-8")
STORE = (ROOT / "shell" / "UsageStore.cs").read_text(encoding="utf-8")
THEME = (ROOT / "shell" / "HbcTheme.cs").read_text(encoding="utf-8")

TABS = ("SuperGrok", "X", "Imagine", "Build", "Control")


def _method(source, signature):
    start = source.find(signature)
    if start < 0:
        return ""
    rest = source[start:]
    nxt = rest.find("\n    private ", len(signature))
    if nxt < 0:
        nxt = rest.find("\n    public ", len(signature))
    if nxt < 0:
        nxt = rest.find("\n    internal ", len(signature))
    return rest if nxt < 0 else rest[:nxt]


def web_page_children(view, bar, state):
    return [view, bar, state]


def usage_limit_default(stored=None):
    if stored is None:
        return ""
    text = str(stored).strip()
    return "" if text == "" else text


class TestDesignApp(unittest.TestCase):
    def test_exactly_five_tabs_in_locked_order(self):
        block = _method(FORM, "private async Task BuildTabs()")
        titles = []
        for match in re.finditer(r'AddWeb\("([^"]+)"', block):
            titles.append(match.group(1))
        if "AddBuild()" in block:
            titles.append("Build")
        if "AddControl()" in block:
            titles.append("Control")
        self.assertEqual(titles, list(TABS))
        self.assertIn('TabOrder = { "SuperGrok", "X", "Imagine", "Build", "Control" }', FORM)

    def test_removed_pages_stay_gone(self):
        self.assertNotIn('AddWeb("Hub"', FORM)
        self.assertNotIn('MakeTab("Hub")', FORM)
        self.assertNotIn('MakeTab("Grok Bot")', FORM)
        self.assertNotIn('MakeTab("Freebots")', FORM)
        self.assertNotIn("Text = \"Grok Bot\"", FORM)
        self.assertNotIn("OpenBot()", FORM)

    def test_add_web_keeps_page_bar_and_state(self):
        add_web = _method(FORM, "private async Task AddWeb(")
        self.assertIn("WebPageLayout.Children<Control>(view, bar, state)", add_web)
        self.assertEqual(web_page_children("view", "bar", "state"), ["view", "bar", "state"])
        layout = (ROOT / "shell" / "WebPageLayout.cs").read_text(encoding="utf-8")
        self.assertIn('{ "view", "bar", "state" }', layout)
        self.assertIn("new[] { view, bar, state }", layout)
        loop = re.search(
            r"foreach \(var child in WebPageLayout\.Children<Control>\(view, bar, state\)\)\s+"
            r"(?:\{\s*)?([^\n\{]+)",
            add_web,
        )
        self.assertIsNotNone(loop, "AddWeb lost the layout foreach")
        self.assertEqual(loop.group(1).strip().rstrip(";"), "page.Controls.Add(child)")
        between = add_web.split("WebPageLayout.Children", 1)[1].split("pages.Add", 1)[0]
        self.assertNotIn("if ", between)
        self.assertNotIn("is WebView", between)
        self.assertRegex(add_web, r"var bar = MakePageBar\(")
        self.assertRegex(add_web, r"var state = MakeTabState\(")

    def test_build_has_no_web_page_bar(self):
        add_build = _method(FORM, "private async Task AddBuild()")
        self.assertNotIn("MakePageBar(", add_build)
        self.assertNotIn("IconButton(\"Back\"", add_build)
        self.assertNotIn("IconButton(\"Forward\"", add_build)
        self.assertNotIn("IconButton(\"Reload\"", add_build)
        self.assertNotIn("Open in browser", add_build)
        self.assertIn("MakeTabState(\"Build\", view, buildUrl, allowOpenBrowser: false)", add_build)
        self.assertIn("build.html?token=", add_build)
        self.assertNotIn("OpenExternal(buildUrl)", add_build)
        self.assertNotIn('MakeTabState("Build", view, HubBase + "/build.html")', add_build)
        make = _method(FORM, "private TabStatePanel MakeTabState(")
        self.assertIn("RestartWebTab", make)
        self.assertIn("HideOpenBrowser()", make)
        self.assertIn("if (allowOpenBrowser)", make)
        self.assertIn("OpenExternal(retryUrl)", make)
        self.assertIn("public void HideOpenBrowser()", STATE)

    def test_each_web_tab_has_loading_or_error_overlay(self):
        self.assertIn("class TabStatePanel", STATE)
        self.assertIn('LoadingText = "Loading"', STATE)
        self.assertIn("This page didn't load", STATE)
        self.assertIn("This tab stopped working", STATE)
        add_web = _method(FORM, "private async Task AddWeb(")
        add_build = _method(FORM, "private async Task AddBuild()")
        for block, name in ((add_web, "AddWeb"), (add_build, "AddBuild")):
            self.assertIn("MakeTabState(", block, name)
            self.assertIn("HookWebTab(", block, name)
            self.assertIn("ShowError", block, name)
            self.assertIn("DefaultBackgroundColor = HbcTheme.Bg", block, name)
        hook = _method(FORM, "private void HookWebTab(")
        self.assertIn("ShowLoaded()", hook)
        self.assertIn("ShowError", hook)
        self.assertIn("OnWebViewProcessFailed", hook)
        self.assertIn("ShowCrashed()", _method(FORM, "private void OnWebViewProcessFailed("))
        self.assertIn("ProcessFailed", hook)
        self.assertIn("Control ready", CONTROL)

    def test_popup_host_one_at_a_time_and_closes_on_blur(self):
        self.assertIn("InitPopupDismiss()", FORM)
        self.assertIn("Deactivate +=", POPUP)
        self.assertIn("DismissPopups()", POPUP)
        self.assertIn("ShouldDismissOnActivateApp", POPUP)
        self.assertIn("ToggleUsagePopup", FORM)
        self.assertIn('Name = "UsagePopupButton"', FORM)
        self.assertIn('Name = "UsagePopup"', FORM)
        self.assertIn('Name = "HubStatus"', FORM)
        self.assertIn('Program.Log("hub connected "', FORM)
        self.assertIn("ToggleHealthPopup", FORM)
        self.assertIn("ToggleMoreMenu", FORM)
        self.assertIn("if (DismissPopups())", FORM)
        self.assertIn("IgnoreChipReopen()", FORM)
        self.assertIn("view.GotFocus += (_, _) => DismissPopups()", FORM)
        self.assertIn("ResizeBegin += (_, _) => DismissPopups()", FORM)
        self.assertIn("TrySelectTabShortcut", POPUP)

    def test_usage_limits_are_user_entered_empty(self):
        self.assertIn("BuildUsageLimitEditor", CONTROL)
        self.assertIn("PlaceholderText = \"used\"", CONTROL)
        self.assertIn("PlaceholderText = \"limit\"", CONTROL)
        self.assertIn("UsageStore.Save", CONTROL)
        self.assertIn("source\"] = \"manual\"", STORE)
        self.assertIn('public string Limit { get; set; } = "";', STORE)
        self.assertEqual(usage_limit_default(), "")
        self.assertEqual(usage_limit_default(None), "")
        self.assertEqual(usage_limit_default(""), "")
        self.assertEqual(usage_limit_default("  "), "")
        self.assertIn("DefaultBlank => \"\"", STORE)
        self.assertIn("? DefaultBlank", STORE)
        limit_box = CONTROL.split("var limit = new TextBox", 1)[1].split("box.Controls.Add(name)", 1)[0]
        self.assertIn('PlaceholderText = "limit"', limit_box)
        self.assertIn("UsageStore.DisplayOrBlank(row?.Limit)", limit_box)
        self.assertNotIn("??", limit_box)
        literals = [
            value
            for value in re.findall(r'(?<!Placeholder)Text\s*=\s*"([^"]*)"', limit_box)
            if value.strip()
        ]
        self.assertEqual(literals, [], "Limit TextBox has a non-empty default: %s" % literals)
        self.assertNotRegex(limit_box, r'Text\s*=\s*[1-9]')
        self.assertNotIn("38 of 100", CONTROL + FORM + STORE)
        self.assertNotIn("Usage 62%", FORM + CONTROL)
        self.assertNotIn("Usage NN%", FORM + CONTROL)

    def test_theme_tokens_and_public_assets_landed(self):
        self.assertIn("#060d1a", THEME)
        self.assertIn("#00e59a", THEME)
        self.assertIn("TitleBarHeight = 44", THEME)
        self.assertIn("TitleBarHeight + HbcTheme.TabBarHeight", FORM)
        self.assertNotIn("TabBarHeight + HbcTheme.TabBarHeight", FORM)
        self.assertTrue((ROOT / "brand" / "lpin-compass-outline.svg").is_file())
        self.assertFalse((ROOT / "brand" / "hbc-mark.svg").exists())
        self.assertTrue((ROOT / "tokens" / "hbc-tokens.css").is_file())
        self.assertTrue((ROOT / "tokens" / "hbc-tokens.json").is_file())
        self.assertTrue((ROOT / "icons" / "supergrok.svg").is_file())
        self.assertTrue((ROOT / "icons" / "x-feed.svg").is_file())
        self.assertIn("MinimumSize = new Size(960, 600)", FORM)
        self.assertIn("Data stays on this PC", FORM)

    def test_usage_button_stays_on_screen_at_960(self):
        self.assertIn('Name = "TitleBar"', FORM)
        self.assertIn("WrapContents = true", FORM)
        self.assertIn("FlowLayoutPanel titleBar", FORM)
        self.assertIn('Name = "UsagePopupButton"', FORM)
        self.assertNotIn("MakeChip(\"Usage\", 1070)", FORM)
        self.assertNotIn("MakeChip(\"⋯\", 1180)", FORM)
        ctor = FORM[FORM.find("public Form1()") : FORM.find("private async Task BuildTabs(")]
        self.assertIn("titleBar.Controls.Add(usageChip)", ctor)
        self.assertIn("WrapContents = true", ctor)

        def wrap_row(widths, width, pad=8, gap=8):
            x = pad
            row = 0
            placed = []
            for w in widths:
                if x + w > width - pad and x > pad:
                    row += 1
                    x = pad
                placed.append((row, x, x + w))
                x += w + gap
            return placed

        chips = wrap_row([72, 64, 36], 960)
        usage = chips[1]
        self.assertLessEqual(usage[2], 960)
        overflow = wrap_row([920, 80, 36], 960)
        self.assertEqual(overflow[1][0], 1)
        self.assertLessEqual(overflow[1][2], 960)
        mutant = FORM.replace("WrapContents = true", "WrapContents = false")
        self.assertNotIn("WrapContents = true", mutant)

    def test_open_external_refuses_token_and_hub(self):
        open_ext = _method(FORM, "private static void OpenExternal(")
        self.assertIn("OpenExternalGuard.Allowed(url, host)", open_ext)
        self.assertIn('Program.Log("open browser refused")', open_ext)
        self.assertIn("new Uri(HubBase).Authority", open_ext)
        guard = (ROOT / "shell" / "OpenExternalGuard.cs").read_text(encoding="utf-8")
        self.assertIn('IndexOf("token=", StringComparison.OrdinalIgnoreCase)', guard)
        self.assertIn("Uri.UnescapeDataString", guard)
        self.assertNotIn("open browser refused \"", open_ext)
        self.assertNotIn("open browser refused \" +", open_ext)

    def test_design_helpers_runtime(self):
        skip_if_no_dotnet(self)
        proj = ROOT / "tests" / "design_helpers_check" / "design_helpers_check.csproj"
        created = subprocess.run(
            [dotnet_path(), "run", "--project", str(proj), "--nologo", "-v", "q"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        out = (created.stdout or "") + (created.stderr or "")
        skip_if_dotnet_unrunnable(self, created.returncode, out, label="design helpers")
        self.assertEqual(created.returncode, 0, out)
        self.assertIn("design-helpers-ok", created.stdout)

    def test_security_hooks_still_present(self):
        self.assertIn("X-Hbc-Token", FORM)
        self.assertIn("HubBase", FORM)
        self.assertIn('host == "127.0.0.1" && target.Port == new Uri(HubBase).Port', FORM)
        self.assertIn("PrepareWebView(view)", FORM)
        self.assertIn("AttachOwnedWebView(view) after Ensure, never before Prepare", FORM)
        self.assertIn("PebRead.CommandLine", FORM)


class TestTabResizeStates(unittest.TestCase):
    def test_helper_keeps_every_tab_nonzero_after_min_max_resize(self):
        def client_for(restored, maximized, state):
            if state == "maximized":
                return maximized
            return (max(restored[0], 1), max(restored[1], 1))

        def tab_host(width, height, top, bottom):
            return (max(width, 1), max(height - max(top, 0) - max(bottom, 0), 1))

        restored = (1280, 860)
        maximized = (1920, 1080)
        top, bottom = 88, 26
        for state, client in (
            ("minimized", restored),
            ("maximized", maximized),
            ("resized", (1100, 700)),
        ):
            used = client_for(restored, maximized, state) if state != "resized" else (1100, 700)
            host = tab_host(used[0], used[1], top, bottom)
            for tab in TABS:
                self.assertGreaterEqual(host[0], 1, "%s %s" % (state, tab))
                self.assertGreaterEqual(host[1], 1, "%s %s" % (state, tab))
        self.assertIn("TabSizeAfterWindowChange", BOUNDS)
        self.assertIn("AllTabs", BOUNDS)
        self.assertIn("SyncShownTab", FORM)
        self.assertIn("WindowState == FormWindowState.Minimized", FORM)

    def test_static_show_and_resize_sync(self):
        select = _method(FORM, "private void SelectTab(int index)")
        self.assertIn("SyncShownTab(pages[index])", select)
        self.assertIn("NudgeIfUnchanged", FORM)
        for title in TABS:
            self.assertIn('"%s"' % title, FORM)


if __name__ == "__main__":
    unittest.main(verbosity=2)
