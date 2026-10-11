"""Usage refresh: API / header / page fixtures. Synthetic examples only."""

import json
import re
import subprocess
import unittest
from pathlib import Path

from tests.runtime_skip import dotnet_path, skip_if_dotnet_unrunnable, skip_if_no_dotnet

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "usage"
LOGIC = (ROOT / "shell" / "UsageRefreshLogic.cs").read_text(encoding="utf-8")
TAB = (ROOT / "shell" / "ControlTab.cs").read_text(encoding="utf-8")
FORM = (ROOT / "shell" / "Form1.cs").read_text(encoding="utf-8")
SECRETS = (ROOT / "shell" / "UsageSecrets.cs").read_text(encoding="utf-8")
DOCS = (ROOT / "docs" / "USAGE-REFRESH.md").read_text(encoding="utf-8")

SCRIPT_BANNED = (
    "click(",
    "submit(",
    "fetch(",
    "XMLHttpRequest",
    "location.assign",
    "location.href",
    "sendBeacon",
    "postMessage",
)

WRITE_METHODS = ("PostAsync", "PutAsync", "DeleteAsync", "HttpMethod.Post", "HttpMethod.Put", "HttpMethod.Delete")


def _dom_script_source():
    start = LOGIC.find("return \"(function()")
    end = LOGIC.find("public static readonly string[] ScriptBanned")
    return LOGIC[start:end]


class TestUsageRefresh(unittest.TestCase):
    def test_fixtures_are_marked_examples(self):
        for path in sorted(FIX.iterdir()):
            if path.name == "README.txt":
                continue
            text = path.read_text(encoding="utf-8")
            self.assertIn("EXAMPLE ONLY", text, path.name)
            self.assertNotRegex(text, r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}")
            self.assertNotIn("sk-", text)

    def test_script_is_read_only(self):
        block = _dom_script_source()
        self.assertIn("querySelectorAll", block)
        self.assertIn("innerText", block)
        for banned in SCRIPT_BANNED:
            self.assertNotIn(banned, block)
        self.assertIn("ScriptIsReadOnly", TAB)
        self.assertIn("ExecuteScriptAsync", TAB)
        self.assertNotIn(".Result", TAB[TAB.find("ReadDomText") : TAB.find("AttachUsageHeaderWatch")])
        self.assertNotIn(".Wait()", TAB[TAB.find("RefreshUsageNow") : TAB.find("ReadWebFeature")])
        self.assertNotIn("GetAwaiter().GetResult()", TAB)

    def test_no_write_http_methods(self):
        collect = TAB[TAB.find("private async Task RefreshUsageNow") : TAB.find("private void ApplyUsageIntervalFromBox")]
        blob = LOGIC + collect + SECRETS
        for banned in WRITE_METHODS:
            self.assertNotIn(banned, blob, banned)
        self.assertIn("HttpMethod.Get", collect)
        self.assertIn("XApiUrl", collect)

    def test_source_labels_and_docs(self):
        self.assertIn('SourceApi = "API"', LOGIC)
        self.assertIn('SourceHeader = "header"', LOGIC)
        self.assertIn('SourcePage = "page"', LOGIC)
        self.assertIn("X API project usage", LOGIC)
        self.assertIn("https://docs.x.com/x-api/usage/introduction", LOGIC)
        self.assertIn("https://api.x.com/2/usage/tweets", LOGIC)
        self.assertIn("| SuperGrok | none public |", DOCS)
        self.assertIn("| Imagine | none public |", DOCS)
        self.assertIn("/api/state", DOCS)
        self.assertIn("x-ratelimit-", DOCS)

    def test_manual_limit_wins(self):
        self.assertIn("manualLimit.Length > 0 ? manualLimit", LOGIC)
        self.assertIn("UsageStore.Load", TAB)
        self.assertIn("await Task.Run(UsageStore.Load)", TAB)
        self.assertIn("UsageRefreshLogic.Merge", TAB)

    def test_interval_off_by_default(self):
        self.assertIn("MinIntervalMinutes = 5", LOGIC)
        self.assertIn("usageRefreshMinutes = 0", FORM)
        self.assertIn("usage-refresh-minutes=", FORM)
        self.assertIn("ShouldRunInterval", TAB)
        self.assertIn("FormWindowState.Minimized", TAB)
        self.assertIn("Interval minutes (0=off, min 5)", TAB)
        self.assertIn("Refresh usage", TAB)

    def test_logs_have_no_secrets(self):
        self.assertIn('return "usage parse " + safeSite + " " + safeStatus', LOGIC)
        self.assertIn("LooksSecret", LOGIC)
        self.assertIn('Program.Log(UsageRefreshLogic.LogLine', TAB)
        self.assertNotIn("Authorization", LOGIC)
        self.assertNotIn("cookie", TAB.lower())
        self.assertIn("ReadXBearer", SECRETS)
        self.assertIn("LPIN/XApiBearer", SECRETS)
        self.assertNotIn("Write-Output", SECRETS)
        self.assertNotIn("Console.Write", SECRETS)

    def test_header_watch_is_read_only(self):
        self.assertIn("WebResourceResponseReceived", TAB)
        watch = TAB[TAB.find("private void AttachUsageHeaderWatch") : TAB.find("private void ApplyUsageIntervalFromBox")]
        self.assertIn("x-ratelimit", LOGIC.lower())
        self.assertIn("RateLimitEndpointOk(e.Request?.Uri)", watch)
        self.assertNotIn("Navigate", watch)
        self.assertNotIn("PostAsync", watch)
        self.assertIn("AttachUsageHeaderWatch(view, tab.Title)", FORM)
        self.assertIn("tabViews[tab.Title] = view", FORM)

    def test_rate_limit_headers_only_from_named_endpoints(self):
        self.assertIn('new("api.x.com", "/2/usage/tweets")', LOGIC)
        self.assertIn("RateLimitEndpointOk", LOGIC)
        self.assertIn("path.Equals(want, StringComparison.Ordinal)", LOGIC)
        self.assertIn("host.Equals(item.Host, StringComparison.OrdinalIgnoreCase)", LOGIC)
        self.assertIn("uri.Port != 443", LOGIC)
        self.assertNotIn("StartsWith", LOGIC[LOGIC.find("RateLimitEndpointOk") : LOGIC.find("ReadRateLimitHeaders")])
        watch = TAB[TAB.find("private void AttachUsageHeaderWatch") : TAB.find("private void ApplyUsageIntervalFromBox")]
        gate = watch.split("ReadRateLimitHeaders")[0]
        self.assertIn("RateLimitEndpointOk", gate)
        self.assertIn("return;", gate)

    def test_parsers_are_separate(self):
        for name in (
            "ParseSuperGrokDom",
            "ParseImagineDom",
            "ParseXReadsDom",
            "ParseXPostsDom",
            "ParseXApi",
            "ParseBuildState",
        ):
            self.assertIn("public static Reading %s" % name, LOGIC)

    def test_python_fixture_shapes(self):
        api = json.loads((FIX / "x-api-present.json").read_text(encoding="utf-8"))
        self.assertEqual(api["data"]["project_usage"], 12)
        self.assertEqual(api["data"]["project_cap"], 10000)
        headers = json.loads((FIX / "headers-present.json").read_text(encoding="utf-8"))
        self.assertEqual(headers["x-ratelimit-limit"], "200")
        self.assertEqual(headers["x-ratelimit-remaining"], "150")
        html = (FIX / "supergrok-present.html").read_text(encoding="utf-8")
        match = re.search(r"(\d+)\s*(?:of|/)\s*(\d+)", html)
        self.assertEqual(match.group(1), "12")
        self.assertEqual(match.group(2), "100")
        missing = (FIX / "supergrok-absent.html").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"(\d+)\s*(?:of|/)\s*(\d+)", missing))
        changed = (FIX / "supergrok-changed.html").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"(\d+)\s*(?:of|/)\s*(\d+)", changed))

    def test_refresh_follows_ui_hang_rules(self):
        tick = TAB[TAB.find("private async void UsageTimerTick") : TAB.find("private async Task RefreshJobs")]
        self.assertIn("if (usageBusy) return", tick)
        now = TAB[TAB.find("private async Task RefreshUsageNow") : TAB.find("private async Task<UsageRefreshLogic.Reading> ReadWebFeature")]
        self.assertIn("await Task.Run(UsageStore.Load)", now)
        self.assertNotIn("File.ReadAllText", now)
        build = TAB[TAB.find("private async Task<UsageRefreshLogic.Reading> ReadBuildState") : TAB.find("private async Task<string> ReadDomText")]
        self.assertIn("await SessionTokenAsync()", build)
        self.assertIn("await SessionTokenAsync(force: true)", build)
        self.assertIn("TokenRejected(response.StatusCode)", build)
        self.assertIn("SendHubState(", build)
        self.assertNotIn("File.ReadAllText", build)
        self.assertNotIn("File.Exists", build)
        dom = TAB[TAB.find("private async Task<string> ReadDomText") : TAB.find("private void AttachUsageHeaderWatch")]
        self.assertIn("Task.WhenAny", dom)
        self.assertIn("TabTimeoutMs", dom)
        self.assertIn("usageTimer?.Stop()", FORM)

    def test_helpers_runtime(self):
        skip_if_no_dotnet(self)
        proj = ROOT / "tests" / "usage_refresh_check" / "usage_refresh_check.csproj"
        created = subprocess.run(
            [dotnet_path(), "run", "--project", str(proj), "--nologo", "-v", "q"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        out = (created.stdout or "") + (created.stderr or "")
        skip_if_dotnet_unrunnable(self, created.returncode, out, label="usage refresh")
        self.assertEqual(created.returncode, 0, out)
        self.assertIn("usage-refresh-ok", created.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
