"""Restart after ProcessFailed must dispose and recreate WebView2 off the UI-block path."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORM = (ROOT / "shell" / "Form1.cs").read_text(encoding="utf-8")
RESTART = (ROOT / "shell" / "WebViewRestart.cs").read_text(encoding="utf-8")
STATE = (ROOT / "shell" / "TabStatePanel.cs").read_text(encoding="utf-8")


def _method(source, signature):
    start = source.find(signature)
    if start < 0:
        return ""
    rest = source[start:]
    nxt = -1
    for token in ("\n    private ", "\n    public ", "\n    internal "):
        at = rest.find(token, len(signature))
        if at >= 0 and (nxt < 0 or at < nxt):
            nxt = at
    return rest if nxt < 0 else rest[:nxt]


def must_recreate_environment(env_missing, browser_process_died):
    return bool(env_missing or browser_process_died)


FINALLY_BLOCK = re.compile(r"finally\s*\{[^{}]*\}", re.S)


def _resets_guard_outside_finally(body, flag):
    stripped = FINALLY_BLOCK.sub("", body)
    return ("%s = false" % flag) in stripped


def _restart_busy_guarded(body):
    return "if (tab.RestartBusy" in body and "RestartBusy = true" in body


def _attaches_after_restart(body):
    attach = body.find("AttachOwnedWebView(view)")
    ensure = body.find("EnsureCoreWebView2Async")
    return attach >= 0 and ensure >= 0 and ensure < attach


class TestWebViewRestart(unittest.TestCase):
    def test_restart_disposes_and_recreates_at_full_client_size(self):
        body = _method(FORM, "private async Task RestartWebTab(")
        self.assertTrue(body)
        self.assertIn("old.Dispose()", body)
        self.assertIn("new WebView2", body)
        self.assertIn("TabHostClientSize()", body)
        self.assertIn("PrepareWebView(view)", body)
        self.assertIn("EnsureCoreWebView2Async", body)
        self.assertIn("HookWebTab(tab)", body)
        self.assertIn("view.Source = new Uri(tab.Url)", body)
        self.assertIn("WebViewRestart.MustRecreateEnvironment", body)
        self.assertIn("BrowserProcessExited", body)
        self.assertIn("ShowError", body)
        self.assertIn("WebViewRestart.TimeoutMs", body)
        self.assertIn("Task.WhenAny", body)
        self.assertIn("Task.Delay", body)
        self.assertNotIn(".Wait()", body)
        self.assertNotIn("GetResult()", body)
        self.assertNotIn("Thread.Sleep", body)
        self.assertNotIn("GetAwaiter()", body)
        self.assertIn("Restart this tab", STATE)
        self.assertIn("ShowCrashed()", STATE)

    def test_retry_uses_restart_path(self):
        make = _method(FORM, "private TabStatePanel MakeTabState(")
        self.assertIn("await RestartWebTab(tab)", make)
        hook = _method(FORM, "private void HookWebTab(")
        self.assertIn("ProcessFailed", hook)
        self.assertIn("OnWebViewProcessFailed", hook)
        self.assertIn("ShowCrashed()", _method(FORM, "private void OnWebViewProcessFailed("))
        self.assertIn("NavigationCompleted", hook)
        self.assertIn("NewWindowRequested", hook)
        self.assertIn("GotFocus", hook)
        add_web = _method(FORM, "private async Task AddWeb(")
        add_build = _method(FORM, "private async Task AddBuild()")
        self.assertIn("HookWebTab(tab)", add_web)
        self.assertIn("HookWebTab(tab)", add_build)

    def test_recreate_environment_when_browser_died(self):
        self.assertTrue(must_recreate_environment(True, False))
        self.assertTrue(must_recreate_environment(False, True))
        self.assertTrue(must_recreate_environment(True, True))
        self.assertFalse(must_recreate_environment(False, False))
        self.assertIn(
            "MustRecreateEnvironment(bool envMissing, bool browserProcessDied)",
            RESTART,
        )
        self.assertIn("envMissing || browserProcessDied", RESTART)
        self.assertIn("TimeoutMs = 15000", RESTART)
        decision = re.search(
            r"MustRecreateEnvironment\(bool envMissing, bool browserProcessDied\)\s*=>\s*"
            r"envMissing \|\| browserProcessDied",
            RESTART,
        )
        self.assertIsNotNone(decision)
        removed = RESTART.replace("envMissing || browserProcessDied", "false")
        self.assertNotIn("envMissing || browserProcessDied", removed)

    def test_restart_timeout_shows_error(self):
        watch = _method(FORM, "private async Task WatchRestartNavigation(")
        self.assertIn("ShowError(\"Restart timed out.\")", watch)
        body = _method(FORM, "private async Task RestartWebTab(")
        self.assertIn("ShowError(\"Restart timed out.\")", body)
        self.assertIn("WatchRestartNavigation", body)

    def test_restart_rebuilds_build_url_from_forced_token(self):
        body = _method(FORM, "private async Task RestartWebTab(")
        self.assertIn("await SessionTokenAsync(force: true)", body)
        self.assertIn(
            'buildUrl = HubBase + "/build.html?token=" + Uri.EscapeDataString(token)',
            body,
        )
        stale = body.replace("await SessionTokenAsync(force: true)", "await SessionTokenAsync()")
        self.assertNotIn("await SessionTokenAsync(force: true)", stale)
        dropped = body.replace(
            'buildUrl = HubBase + "/build.html?token=" + Uri.EscapeDataString(token);',
            "",
        )
        self.assertNotIn("Uri.EscapeDataString(token)", dropped)

    def test_restart_busy_guard_and_finally_reset(self):
        body = _method(FORM, "private async Task RestartWebTab(")
        self.assertTrue(_restart_busy_guarded(body))
        self.assertFalse(_resets_guard_outside_finally(body, "tab.RestartBusy"))
        unguarded = body.replace("if (tab.RestartBusy || IsDisposed) return;", "")
        self.assertFalse(_restart_busy_guarded(unguarded))
        outside = body.replace(
            "finally\n        {\n            tab.RestartBusy = false;\n        }",
            "tab.RestartBusy = false;",
        )
        self.assertTrue(_resets_guard_outside_finally(outside, "tab.RestartBusy"))

    def test_restart_attaches_owned_webview(self):
        body = _method(FORM, "private async Task RestartWebTab(")
        self.assertTrue(_attaches_after_restart(body))
        ensure_await = body.find("await ensure;")
        attach = body.find("await AttachOwnedWebView(view);")
        hook = body.find("HookWebTab(tab)")
        self.assertGreater(ensure_await, -1)
        self.assertGreater(attach, ensure_await)
        self.assertGreater(hook, attach)
        missing = body.replace("await AttachOwnedWebView(view);", "")
        self.assertFalse(_attaches_after_restart(missing))
        self.assertEqual(missing.find("await AttachOwnedWebView(view);"), -1)
        self.assertGreater(missing.find("HookWebTab(tab)"), -1)

    def test_process_failed_ignores_old_or_disposed_view(self):
        hook = _method(FORM, "private void HookWebTab(")
        failed = hook[hook.find("ProcessFailed") : hook.find("NavigationCompleted")]
        self.assertNotIn("view.CoreWebView2", failed)
        self.assertIn("capturedCore", failed)
        guard = "if (view.IsDisposed || !ReferenceEquals(tab.View, view)) return;"
        self.assertIn(guard, failed)
        self.assertIn("OnWebViewProcessFailed(tab, view, e.ProcessFailedKind)", failed)
        self.assertIn("ReferenceEquals(sender, capturedCore)", failed)
        self.assertIn("ReferenceEquals(sender, view)", failed)
        dropped = failed.replace(guard, "", 1)
        self.assertNotIn("view.IsDisposed", dropped.split("OnWebViewProcessFailed", 1)[0])
        through_core = failed.replace("capturedCore", "view.CoreWebView2")
        self.assertIn("view.CoreWebView2", through_core)

    def test_process_failed_every_kind_and_browser_exited(self):
        form = FORM
        self.assertIn("OnWebViewProcessFailed", form)
        self.assertIn("HookBrowserProcessExited", form)
        self.assertIn("BrowserProcessExited", form)
        self.assertIn("EnvExitHook", form)
        hook = _method(form, "private void HookWebTab(")
        self.assertIn("HookBrowserProcessExited(tab)", hook)
        self.assertLess(hook.find("HookBrowserProcessExited"), hook.find("ProcessFailed"))
        died = _method(form, "private void OnWebViewProcessFailed(")
        self.assertIn("tab.LastFail = kind", died)
        self.assertIn("ShowCrashed()", died)
        self.assertNotIn("view.CoreWebView2", died)
        self.assertNotIn("if (kind ==", died)
        self.assertNotIn("ProcessFailedKind.BrowserProcessExited)", died.split("ShowCrashed", 1)[0])
        env = _method(form, "private void HookBrowserProcessExited(")
        self.assertIn("env.BrowserProcessExited", env)
        self.assertIn("CoreWebView2ProcessFailedKind.BrowserProcessExited", env)
        gone = hook.replace("HookBrowserProcessExited(tab);", "")
        self.assertNotIn("HookBrowserProcessExited(tab)", gone)

    def test_restart_success_hides_crashed_overlay(self):
        body = _method(FORM, "private async Task RestartWebTab(")
        self.assertIn("tab.LastFail = null", body)
        self.assertLess(
            body.find("tab.LastFail = null"),
            body.find("view.Source = new Uri(tab.Url)"),
        )
        hook = _method(FORM, "private void HookWebTab(")
        nav = hook[hook.find("NavigationCompleted") :]
        self.assertIn("e.IsSuccess", nav)
        self.assertIn("tab.LastFail = null", nav)
        self.assertIn("ShowLoaded()", nav)
        self.assertLess(nav.find("e.IsSuccess"), nav.find("tab.LastFail = null"))
        self.assertLess(nav.find("tab.LastFail = null"), nav.find("ShowLoaded()"))

    def test_build_process_failed_keeps_crashed_overlay(self):
        hook = _method(FORM, "private void HookWebTab(")
        nav = hook[hook.find("NavigationCompleted") :]
        self.assertIn("tab.LastFail is not null", nav)
        self.assertIn("ShowCrashed()", nav)
        self.assertIn("else if (tab.LastFail is not null)", nav)
        self.assertIn("if (e.IsSuccess)", nav)
        self.assertLess(nav.find("LastFail"), nav.find("ShowLoaded()"))
        self.assertLess(nav.find("LastFail"), nav.find("tab.IsBuild"))
        dropped = hook.replace("if (tab.LastFail is not null", "if (false")
        dropped_nav = dropped[dropped.find("NavigationCompleted") :]
        self.assertNotIn("tab.LastFail is not null", dropped_nav)
        add_build = _method(FORM, "private async Task AddBuild()")
        self.assertIn("WebPageLayout.Children<Control>(view, banner, state)", add_build)
        self.assertIn("state.BringToFront()", add_build)
        self.assertIn("HookWebTab(tab)", add_build)
        self.assertIn("OnWebViewProcessFailed", hook)
        self.assertIn("e.ProcessFailedKind", hook)
        self.assertIn("ShowCrashed()", _method(FORM, "private void OnWebViewProcessFailed("))

    def test_process_failed_requires_handle_created(self):
        died = _method(FORM, "private void OnWebViewProcessFailed(")
        handle = "if (IsDisposed || !IsHandleCreated) return;"
        self.assertIn(handle, died)
        before = died.split("BeginInvoke", 1)[0]
        self.assertIn("IsHandleCreated", before)
        self.assertLess(died.find(handle), died.find("BeginInvoke"))
        dropped = died.replace(handle, "", 1)
        self.assertNotIn("IsHandleCreated", dropped.split("BeginInvoke", 1)[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
