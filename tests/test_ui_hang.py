"""Static UI-thread hang guards: Tick I/O, debounce, token cache, watchdog."""

import asyncio
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORM = (ROOT / "shell" / "Form1.cs").read_text(encoding="utf-8")
TAB = (ROOT / "shell" / "ControlTab.cs").read_text(encoding="utf-8")
LOGIC = (ROOT / "shell" / "ControlLogic.cs").read_text(encoding="utf-8")
PROGRAM = (ROOT / "shell" / "Program.cs").read_text(encoding="utf-8")
WATCH = (ROOT / "shell" / "UiWatchdog.cs").read_text(encoding="utf-8")
SHELL = FORM + "\n" + TAB

TICKS = (
    "private async void OnHubTick(",
    "private async void OnHealthTick(",
    "private async void OnJobTick(",
    "private async void OnFeedDebounceTick(",
)
WORK = (
    "private async Task RefreshHub()",
    "private Task RefreshBuildBadge()",
    "private async Task RefreshHealth()",
    "private async Task RefreshUsage()",
    "private async Task RefreshJobs()",
    "private async Task RefreshFeed()",
    "internal static async Task<bool> HubUp()",
    "private void ApplyBuildHealth(",
)

BANNED = re.compile(
    r"\bFile\.(Exists|ReadAll|WriteAll|Append|Open|Read|Create)|"
    r"\bProcess\.GetProcesses|GetProcessesByName|"
    r"\bPebRead\.|ReadProcessMemory|NtQueryInformationProcess|"
    r"\bBuildWatcherRunning\(|VisibleGrokBuildTerminal\("
)
BANNED_DISK = re.compile(
    r"\b(JobBoard|ReadyQueue|StatusFeed|ReadEvents|LoadRefresh|LoadFeed|ReadFile|UsageRows|UsageStore\.Load)\s*\("
)
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


def _before_await(body):
    idx = body.find("await ")
    return body if idx < 0 else body[:idx]


def _first_await_callee(body):
    match = re.search(r"await\s+([A-Z][A-Za-z0-9]*)\s*\(", body)
    return match.group(1) if match else None


def _hits(body, extra=None):
    found = []
    for regex in (BANNED, BANNED_DISK) + ((extra,) if extra else ()):
        match = regex.search(body)
        if match:
            found.append(match.group(0))
    return found


def _lookup(name):
    for sig in (
        "private async void %s(" % name,
        "private async Task %s(" % name,
        "private async Task %s()" % name,
        "internal static bool %s(" % name,
        "internal Task<string> %s(" % name,
        "private Task %s()" % name,
        "private void %s(" % name,
        "internal static async Task<bool> %s()" % name,
        "internal string %s(" % name,
    ):
        body = _method(SHELL, sig)
        if body:
            return body
    return ""


FINALLY_BLOCK = re.compile(r"finally\s*\{[^{}]*\}", re.S)


def _resets_guard_outside_finally(body, flag):
    stripped = FINALLY_BLOCK.sub("", body)
    return ("%s = false" % flag) in stripped


def _watcher_calls_refresh_feed(src):
    watch = _method(src, "private void WatchStatusFeed()")
    post = _method(src, "private void PostFeedRefresh()")
    return "RefreshFeed" in watch or "RefreshFeed" in post


class TestUiHang(unittest.TestCase):
    def test_tick_handlers_have_inflight_guards(self):
        self.assertIn("hubTimer.Tick += OnHubTick", FORM)
        self.assertIn("healthTimer.Tick += OnHealthTick", TAB)
        self.assertIn("jobTimer.Tick += OnJobTick", TAB)
        self.assertIn("feedDebounce.Tick += OnFeedDebounceTick", TAB)
        for sig, flag in (
            ("private async void OnHubTick(", "hubTickBusy"),
            ("private async void OnHealthTick(", "healthTickBusy"),
            ("private async void OnJobTick(", "jobTickBusy"),
        ):
            body = _method(SHELL, sig)
            self.assertTrue(body, sig)
            self.assertIn("if (%s) return" % flag, body, flag)
            self.assertIn("%s = true" % flag, body, flag)
            self.assertIn("%s = false" % flag, body, flag)
            self.assertFalse(_resets_guard_outside_finally(body, flag), flag)
        feed = _method(TAB, "private async Task RefreshFeed()")
        self.assertIn("if (IsDisposed || feedTickBusy) return", feed)
        self.assertIn("feedTickBusy = true", feed)
        self.assertIn("finally { feedTickBusy = false; }", feed)
        self.assertFalse(_resets_guard_outside_finally(feed, "feedTickBusy"))
        debounce = _method(TAB, "private async void OnFeedDebounceTick(")
        self.assertIn("await RefreshFeed()", debounce)
        self.assertNotIn("feedTickBusy", debounce)

    def test_tick_sync_prefix_has_no_file_or_process_io(self):
        seen = []
        for sig in TICKS:
            body = _method(SHELL, sig)
            self.assertTrue(body, sig)
            prefix = _before_await(body)
            hits = _hits(prefix)
            self.assertEqual(hits, [], "%s before await: %s" % (sig, hits))
            name = _first_await_callee(body)
            walked = []
            while name and name not in walked:
                walked.append(name)
                callee = _lookup(name)
                if not callee:
                    break
                child_hits = _hits(_before_await(callee))
                self.assertEqual(
                    child_hits, [], "%s -> %s before await: %s" % (sig, name, child_hits)
                )
                seen.append(name)
                name = _first_await_callee(callee)
        self.assertIn("RefreshHub", seen)
        self.assertIn("RefreshHealth", seen)
        self.assertIn("HubUp", seen)

    def test_work_methods_before_await_stay_off_disk(self):
        for sig in WORK:
            body = _method(SHELL, sig)
            self.assertTrue(body, sig)
            hits = _hits(_before_await(body))
            self.assertEqual(hits, [], "%s before await: %s" % (sig, hits))

    def test_refresh_health_drops_peb_and_offloads_events(self):
        health = _method(TAB, "private async Task RefreshHealth()")
        self.assertNotIn("BuildWatcherRunning", health)
        self.assertNotIn("VisibleGrokBuildTerminal", health)
        self.assertNotIn("GetProcessesByName", health)
        self.assertNotIn("PebRead", health)
        self.assertIn("build_pane", health)
        self.assertIn("await Task.Run(() => ControlLogic.ReadEvents(root, 10))", health)
        self.assertIn("BeginInvoke(applyEvents)", health)
        self.assertIn("ApplyBuildHealth(lastBuildPane)", health)
        self.assertIn("internal bool BuildWatcherRunning(bool buildPane)", TAB)
        self.assertIn("VisibleGrokBuildTerminal", FORM + TAB)
        start = _method(FORM, "private async Task StartAll()")
        self.assertIn("await Task.Run(() => BuildSessionOpen())", start)
        self.assertIn("await Task.Run(() => FocusBuild())", start)
        self.assertNotIn(".Wait()", start)
        self.assertNotIn("GetResult()", start)

    def test_jobs_and_feed_off_ui_thread(self):
        jobs = _method(TAB, "private async Task RefreshJobs()")
        feed = _method(TAB, "private async Task RefreshFeed()")
        self.assertIn("await Task.Run(() => ControlLogic.LoadRefresh(handoff, approvals))", jobs)
        self.assertIn("BeginInvoke(applyJobs)", jobs)
        self.assertNotIn("JobBoard(", jobs)
        self.assertNotIn("ReadyQueue(", jobs)
        self.assertIn("await Task.Run(() => ControlLogic.LoadFeed(handoff))", feed)
        self.assertIn("BeginInvoke(applyFeed)", feed)
        self.assertNotIn("StatusFeed(", feed)
        self.assertNotIn("File.Exists", feed)
        needs = _method(TAB, "private void RefreshNeeds()")
        self.assertIn("ControlLogic.NeedsYou(readyRows)", needs)
        self.assertNotIn("ReadyQueue(", needs)

    def test_readyqueue_reuses_one_jobboard_and_one_approvals_read(self):
        load = _method(LOGIC, "internal static RefreshSnapshot LoadRefresh(")
        self.assertIn("ReadFile(approvalsPath)", load)
        self.assertEqual(load.count("ReadFile(approvalsPath)"), 1)
        self.assertIn("JobBoardFromState", load)
        self.assertIn("ReadyFromJobs(jobs, state)", load)
        self.assertNotIn("ApprovalsState(approvalsPath)", load)
        self.assertNotIn("ApprovalsHistory(approvalsPath)", load)
        ready = _method(LOGIC, "internal static List<NeedRow> ReadyQueue(")
        self.assertIn("LoadRefresh(handoff, approvals).Ready", ready)
        self.assertNotIn("JobBoard(", ready)
        self.assertNotIn("HeldFromApprovals", ready)
        from_jobs = _method(LOGIC, "internal static List<NeedRow> ReadyFromJobs(")
        self.assertIn("row.Status != \"queued\"", from_jobs)
        board = _method(LOGIC, "internal static List<JobRow> JobBoard(")
        self.assertIn("LoadRefresh(handoff, approvals, limit).Jobs", board)

    def test_feed_watcher_debounces_500ms(self):
        self.assertIn("internal const int FeedDebounceMs = 500", TAB)
        self.assertIn("Interval = FeedDebounceMs", TAB)
        watch = _method(TAB, "private void WatchStatusFeed()")
        self.assertIn("PostFeedRefresh()", watch)
        self.assertFalse(_watcher_calls_refresh_feed(TAB))
        post = _method(TAB, "private void PostFeedRefresh()")
        self.assertIn("IsDisposed", post)
        self.assertIn("IsHandleCreated", post)
        self.assertIn("BeginInvoke(ScheduleFeedRefresh)", post)
        self.assertIn("ObjectDisposedException", post)
        self.assertIn("InvalidOperationException", post)
        sched = _method(TAB, "private void ScheduleFeedRefresh()")
        self.assertIn("feedDebounce.Stop()", sched)
        self.assertIn("feedDebounce.Start()", sched)
        self.assertIn("OnFeedDebounceTick", sched)

    def test_session_token_cache_rereads_on_start_miss_or_reject(self):
        token = _method(FORM, "internal string SessionToken(")
        self.assertIn(".session-token", token)
        self.assertIn("if (force)", token)
        self.assertIn("if (sessionTokenLoaded) return sessionTokenCache ?? \"\"", token)
        self.assertIn("File.ReadAllText(tokenPath)", token)
        async_tok = _method(FORM, "internal Task<string> SessionTokenAsync(")
        self.assertIn("Task.Run(() => SessionToken(force))", async_tok)
        start = _method(FORM, "private async Task StartHub()")
        self.assertIn("await SessionTokenAsync(force: true)", start)
        badge = _method(FORM, "private Task RefreshBuildBadge()")
        self.assertNotIn("File.ReadAllText", badge)
        self.assertNotIn("File.Exists", badge)
        self.assertNotIn("SessionTokenAsync", badge)
        self.assertNotIn("SendHubState", badge)
        recover = _method(FORM, "private async Task RefreshBuildTokenAfterHubUp()")
        self.assertIn("await SessionTokenAsync(force: true)", recover)
        rejected = _method(FORM, "internal static bool TokenRejected(")
        self.assertIn("HttpStatusCode.Unauthorized", rejected)
        self.assertIn("HttpStatusCode.Forbidden", rejected)
        restart = _method(TAB, "internal async Task<bool> RestartBuildPaneAsync")
        self.assertIn("await SessionTokenAsync()", restart)
        self.assertIn("TokenRejected(response.StatusCode)", restart)
        self.assertEqual(FORM.count("File.ReadAllText(tokenPath)"), 1)
        self.assertEqual(TAB.count("File.ReadAllText"), 0)
        usage = _method(TAB, "private async Task RefreshUsage()")
        self.assertIn("await Task.Run(() => ControlLogic.UsageRows())", usage)
        self.assertIn("UsageStore.Load()", _method(LOGIC, "internal static List<UsageRow> UsageRows()"))

    def test_state_bad_token_is_403_and_triggers_one_reread(self):
        from aiohttp.test_utils import TestClient, TestServer
        from tests.live_guard import install_live_state_guard, point_hub_at, restore_hub
        import server

        install_live_state_guard()
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        prev = point_hub_at(home)
        prev_token = server.TOKEN
        try:
            server.TOKEN = "a" * 32

            async def run():
                app = server.make_app()
                async with TestClient(TestServer(app)) as client:
                    missing = await client.get(
                        "/api/state", headers={"Host": "127.0.0.1:8107"}
                    )
                    wrong = await client.get(
                        "/api/state",
                        headers={"Host": "127.0.0.1:8107", "X-Hbc-Token": "b" * 32},
                    )
                    return missing.status, wrong.status

            missing_status, wrong_status = asyncio.run(run())
        finally:
            server.TOKEN = prev_token
            restore_hub(prev)
            tmp.cleanup()

        self.assertEqual(missing_status, 403)
        self.assertEqual(wrong_status, 403)
        self.assertNotEqual(missing_status, 401)
        rejected = _method(FORM, "internal static bool TokenRejected(")
        self.assertIn("Forbidden", rejected)
        self.assertIn("Unauthorized", rejected)
        only_401 = rejected.replace(
            "\n        || code == System.Net.HttpStatusCode.Forbidden", ""
        )
        self.assertNotIn("Forbidden", only_401)
        badge = _method(FORM, "private Task RefreshBuildBadge()")
        self.assertNotIn("TokenRejected", badge)
        self.assertNotIn("SendHubState", badge)

    def test_program_log_is_queued_and_fatal_is_sync(self):
        log = _method(PROGRAM, "internal static void Log(")
        self.assertIn("EnqueueLine", log)
        self.assertNotIn("File.AppendAllText", log)
        fatal = _method(PROGRAM, "internal static void LogFatal(")
        self.assertIn("WriteLine", fatal)
        write = _method(PROGRAM, "private static void WriteLine(")
        self.assertIn("File.AppendAllText", write)
        self.assertIn('LogFatal("UI "', PROGRAM)
        self.assertIn('LogFatal("DOMAIN "', PROGRAM)
        self.assertIn("LogQueueCap = 256", PROGRAM)
        enqueue = _method(PROGRAM, "private static void EnqueueLine(")
        self.assertIn("LogQueue.Count > LogQueueCap", enqueue)
        self.assertIn("TryDequeue", enqueue)
        self.assertIn("Interlocked.Increment(ref droppedLogs)", enqueue)
        self.assertIn('Log("run returned")', PROGRAM)
        flush = _method(PROGRAM, "internal static void FlushLog()")
        self.assertIn("DrainQueue()", flush)
        self.assertNotIn("LogDone", PROGRAM)
        loop = _method(PROGRAM, "private static void LogLoop()")
        self.assertIn("while (true)", loop)

    def test_watchdog_wiring_stack_free(self):
        self.assertIn("UiWatchdog.Start(this)", FORM)
        self.assertIn("HandleCreated += (_, _) => UiWatchdog.Start(this)", FORM)
        self.assertIn("internal const int PingMs = 1000", WATCH)
        self.assertIn("internal const int HangMs = 4000", WATCH)
        self.assertIn("ui.BeginInvoke((Action)Ping)", WATCH)
        self.assertIn("Thread.Sleep(PingMs)", WATCH)
        self.assertIn("\"UI hang suspected, \"", WATCH)
        self.assertIn("lag / 1000", WATCH)
        self.assertIn("Volatile.Write(ref lastPump", WATCH)
        self.assertIn("Volatile.Read(ref lastPump)", WATCH)
        self.assertNotIn("StackTrace", WATCH)
        self.assertNotIn("MessageBox", WATCH)
        self.assertNotIn("GetResult", WATCH)
        self.assertNotIn(".Wait(", WATCH)
        start = _method(WATCH, "internal static void Start(")
        self.assertIn("IsBackground = true", start)
        self.assertNotIn("Invoke(", start.replace("BeginInvoke", ""))

    def test_build_pane_label_is_hub_pane_alive(self):
        apply_health = _method(TAB, "private void ApplyBuildHealth(")
        self.assertIn("hub pane alive", apply_health)
        self.assertIn("hub pane down", apply_health)
        self.assertNotIn("build on", apply_health)
        self.assertNotIn("build off", apply_health)

    def test_mutants_catch_regressions(self):
        health = _method(TAB, "private async Task RefreshHealth()")
        poisoned = health.replace(
            "ApplyBuildHealth(lastBuildPane);",
            "var buildUp = BuildWatcherRunning(lastBuildPane);\n        ApplyBuildHealth(buildUp);",
            1,
        )
        self.assertTrue(_hits(_before_await(poisoned)))
        tick = _method(FORM, "private async void OnHubTick(")
        leaked = tick.replace("if (hubTickBusy) return;", "File.ReadAllText(root);\n        if (hubTickBusy) return;")
        self.assertTrue(_hits(_before_await(leaked)))
        self.assertIn("if (hubTickBusy) return", tick)
        ready = _method(LOGIC, "internal static List<NeedRow> ReadyQueue(")
        old = ready.replace(
            "LoadRefresh(handoff, approvals).Ready",
            "ReadyFromJobs(JobBoard(handoff, approvals), ApprovalsState(approvals))",
        )
        self.assertIn("JobBoard(", old)
        self.assertFalse(_watcher_calls_refresh_feed(TAB))
        bypass = TAB.replace("BeginInvoke(ScheduleFeedRefresh)", "BeginInvoke(RefreshFeed)")
        self.assertTrue(_watcher_calls_refresh_feed(bypass))
        direct = TAB.replace("(_, _) => PostFeedRefresh()", "(_, _) => RefreshFeed()")
        self.assertTrue(_watcher_calls_refresh_feed(direct))
        self.assertFalse(_resets_guard_outside_finally(tick, "hubTickBusy"))
        outside = tick.replace(
            "try { await RefreshHub(); }\n        finally { hubTickBusy = false; }",
            "try { await RefreshHub(); }\n        hubTickBusy = false;",
        )
        self.assertTrue(_resets_guard_outside_finally(outside, "hubTickBusy"))
        token = _method(FORM, "internal string SessionToken(")
        self.assertIn("sessionTokenLoaded", token)
        watch_src = WATCH.replace("ui.BeginInvoke((Action)Ping)", "Ping()")
        self.assertNotIn("BeginInvoke", watch_src)
        rejected = _method(FORM, "internal static bool TokenRejected(")
        only_401 = rejected.replace(
            "\n        || code == System.Net.HttpStatusCode.Forbidden", ""
        )
        self.assertNotIn("Forbidden", only_401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
