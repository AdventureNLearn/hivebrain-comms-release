"""Static shell tab list and leftover-control checks after Hub/Grok Bot/Freebots cut."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORM = (ROOT / "shell" / "Form1.cs").read_text(encoding="utf-8")
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")
BUILD_HTML = (ROOT / "build.html").read_text(encoding="utf-8")

EXPECTED_TABS = ["SuperGrok", "X", "Imagine", "Build", "Control"]
REMOVED_TITLES = ("Hub", "Grok Bot", "Freebots")


def _method(source, signature):
    token = signature
    start = source.find(token)
    if start < 0:
        return ""
    rest = source[start:]
    nxt = rest.find("\n    private ", len(token))
    if nxt < 0:
        nxt = rest.find("\n    public ", len(token))
    return rest if nxt < 0 else rest[:nxt]


def _build_tabs():
    return _method(FORM, "private async Task BuildTabs()")


def _tab_titles():
    block = _build_tabs()
    titles = []
    for match in re.finditer(r'AddWeb\("([^"]+)"', block):
        titles.append(match.group(1))
    if re.search(r"\bAddBuild\s*\(\s*\)", block):
        titles.append("Build")
    if re.search(r"\bAddControl\s*\(\s*\)", block):
        titles.append("Control")
    if re.search(r"\bAddRouting\s*\(\s*\)", block):
        titles.append("Grok Bot")
    return titles


class TestShellTabs(unittest.TestCase):
    def test_tab_list_is_exactly_supergrok_x_imagine_build_control(self):
        self.assertEqual(_tab_titles(), EXPECTED_TABS)
        self.assertIn('MakeTab("Build")', FORM)
        self.assertNotIn("AddRouting", FORM)
        for title in REMOVED_TITLES:
            self.assertNotIn('AddWeb("%s"' % title, FORM)
            self.assertNotIn('MakeTab("%s")' % title, FORM)

    def test_removed_tab_controls_are_gone(self):
        self.assertNotIn("AddRouting", FORM)
        self.assertNotIn("TailOf(", FORM)
        self.assertNotIn("https://freebots.lol/", FORM)
        self.assertNotIn("freebots.lol", FORM)
        self.assertNotIn('"freebots"', FORM)
        self.assertNotIn('Path.Combine(root, "profiles", "lab")', FORM)
        self.assertNotIn('Path.Combine(root, "profiles", "freebots")', FORM)
        self.assertNotIn("Directory.Delete", FORM)
        self.assertNotIn("hubWeb", FORM)
        for title in REMOVED_TITLES:
            self.assertNotIn('IndexOf("%s")' % title, FORM)
        add_web = _method(FORM, "private async Task AddWeb(")
        self.assertIn('Path.Combine(root, "profiles", profile)', add_web)
        self.assertIn("Directory.CreateDirectory(folder)", add_web)
        self.assertNotIn("Directory.Delete", add_web)

    def test_start_grok_confirm_mint_and_start_stop_use_live_controls(self):
        for name in (
            "private async Task ToggleHub()",
            "private async Task StartHub()",
            "private void StopHub()",
            "private async Task RefreshHub()",
            "internal static async Task<bool> HubUp()",
        ):
            self.assertIn(name, FORM)
        self.assertIn("WaitForHubAsync", FORM)
        self.assertIn("HubUpProbe", FORM)
        self.assertIn('Program.Log("hub connected "', FORM)
        self.assertIn('Name = "HubStatus"', FORM)
        wait = _method(FORM, "internal static async Task<(bool up, int ms)> WaitForHubAsync(")
        self.assertIn("HubUpProbe()", wait)
        self.assertIn("Task.Delay", wait)
        self.assertIn("delay * 2", wait)
        self.assertNotIn(".Wait()", wait)
        self.assertNotIn("GetResult()", wait)
        self.assertIn("hubToggle", FORM)
        self.assertIn("hubTimer", FORM)
        self.assertIn("await RefreshHub()", FORM)
        self.assertIn("hubTimer.Tick += OnHubTick", FORM)
        self.assertIn("if (hubTickBusy) return", FORM)
        self.assertIn(".session-token", FORM)
        add_build = _method(FORM, "private async Task AddBuild()")
        self.assertIn("SessionTokenAsync()", add_build)
        token = _method(FORM, "internal string SessionToken(")
        self.assertIn(".session-token", token)
        self.assertIn("build.html?token=", add_build)
        self.assertIn('MakeTab("Build")', add_build)
        self.assertIn("HubBase + \"/build.html?token=\"", add_build)
        self.assertIn("buildUrl", add_build)
        self.assertIn("AddControl()", FORM)
        make_tab = _method(FORM, "private Button MakeTab(")
        self.assertIn("Tag = title", make_tab)
        index_of = _method(FORM, "private int IndexOf(")
        self.assertIn("(tabButtons[i].Tag as string) == title", index_of)
        self.assertNotIn("tabButtons[i].Text == title", index_of)
        start_all = _method(FORM, "private async Task StartAll()")
        self.assertIn('SelectTab(IndexOf("Build"))', start_all)
        self.assertIn("await StartHub()", start_all)
        restore = _method(FORM, "private void RestoreTab()")
        self.assertIn('IndexOf("SuperGrok")', restore)
        self.assertIn("ensure_session_token", SERVER)
        self.assertIn('/term/{pane}', SERVER)
        self.assertIn("/term/", BUILD_HTML)
        self.assertIn("token", BUILD_HTML)

        confirm = _method(FORM, "private async Task ConfirmStartGrok()")
        mint = _method(FORM, "private async Task MintTermTickets(")
        if confirm:
            self.assertIn("/api/nonce", confirm)
            self.assertIn("start-grok", confirm)
            self.assertIn("hubToggle", FORM)
            self.assertNotIn('IndexOf("Hub")', confirm)
            self.assertNotIn("hubWeb", confirm)
            self.assertNotIn("AddRouting", confirm)
        if mint:
            self.assertIn("/api/nonce", mint)
            self.assertIn("buildWeb", mint)
            self.assertNotIn('IndexOf("Hub")', mint)
            self.assertNotIn("hubWeb", mint)
            self.assertNotIn("AddRouting", mint)


if __name__ == "__main__":
    unittest.main(verbosity=2)
