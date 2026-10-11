"""Build-tab approvals panel, decide path, and waiting-job badge."""

import asyncio
import inspect
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server
from tests.live_guard import install_live_state_guard, point_hub_at, restore_hub

install_live_state_guard()

FORM = (ROOT / "shell" / "Form1.cs").read_text(encoding="utf-8")
BUILD = (ROOT / "build.html").read_text(encoding="utf-8")
DESK = (ROOT / "desk.js").read_text(encoding="utf-8")
GITIGNORE = (ROOT / ".gitignore").read_text(encoding="utf-8")
HUB_HOST = "127.0.0.1:8107"


def _method(source, signature):
    start = source.find(signature)
    if start < 0:
        return ""
    rest = source[start:]
    nxt = rest.find("\n    private ", len(signature))
    if nxt < 0:
        nxt = rest.find("\n    public ", len(signature))
    if nxt < 0:
        nxt = rest.find("\nasync function ", len(signature))
    if nxt < 0:
        nxt = rest.find("\nfunction ", len(signature))
    return rest if nxt < 0 else rest[:nxt]


def _js_fn(source, name):
    token = "async function %s" % name
    start = source.find(token)
    if start < 0:
        token = "function %s" % name
        start = source.find(token)
    if start < 0:
        return ""
    rest = source[start:]
    nxt = rest.find("\nasync function ", len(token))
    if nxt < 0:
        nxt = rest.find("\nfunction ", len(token))
    return rest if nxt < 0 else rest[:nxt]


def _state_fetches(source):
    return re.findall(r'fetch\((["\'])(/api/state[^"\']*)\1', source)


def _decide_fetches(source):
    return re.findall(r'fetch\((["\'])(/api/decide[^"\']*)\1', source)


def _decide_ok(src):
    if 'fetch("/api/decide"' not in src and "fetch('/api/decide'" not in src:
        return False
    if 'method: "POST"' not in src and "method: 'POST'" not in src:
        return False
    if "X-Hbc-Token" not in src:
        return False
    if "JSON.stringify({ id, decision, hash })" not in src:
        return False
    if re.search(r"/api/decide\?token=", src):
        return False
    if "JSON.stringify({ id, decision, lane" in src:
        return False
    if 'method: "GET"' in src or "method: 'GET'" in src:
        return False
    if "setDecideButtons(false)" not in src:
        return False
    if "decideBusy = true" not in src:
        return False
    if "if (decideBusy) return" not in src:
        return False
    return True


def _build_decide_ok(src):
    if not _decide_ok(src):
        return False
    if '"X-Hbc-Token": currentToken()' not in src:
        return False
    if re.search(r'"X-Hbc-Token":\s*token\b', src):
        return False
    return True


def _hub_build_origin_ok(form):
    recover = _method(form, "private async Task RefreshBuildTokenAfterHubUp()")
    helper = _method(form, "private static bool IsHubBuildPage(")
    if "IsHubBuildPage(buildView.Source)" not in recover:
        return False
    if "core.Source" not in recover:
        return False
    if "IsHubBuildPage(live)" not in recover:
        return False
    script = recover.find("ExecuteScriptAsync")
    if script < 0 or "IsHubBuildPage(live)" not in recover[:script]:
        return False
    if "window.__hbcToken" not in recover:
        return False
    if 'source.Host == "127.0.0.1"' not in helper:
        return False
    if "source.Port == hub.Port" not in helper:
        return False
    if '"/build.html"' not in helper:
        return False
    if "Uri.UriSchemeHttp" not in helper:
        return False
    if "source.AbsolutePath" not in helper:
        return False
    return True


def _job_fields_textcontent(src):
    load = _js_fn(src, "loadApprovals") or _js_fn(src, "load")
    if re.search(r"innerHTML\s*=\s*[^;]*job\.(id|from|summary|lane)", load):
        return False
    if re.search(r"innerHTML\s*=", load) and re.search(r"job\.(id|from|summary)", load):
        # assignment of job.id / job.from / job.summary via innerHTML
        for field in ("id", "from", "summary"):
            if re.search(r"innerHTML[\s\S]{0,80}job\.%s" % field, load):
                return False
    if "textContent = job.id" not in load and "title.textContent =" not in load:
        return False
    if "job.from" not in load and "job.summary" not in load:
        return False
    return True


def _desk_header_ok(src):
    load = _js_fn(src, "load")
    if "X-Hbc-Token" not in load:
        return False
    if "/api/state?token=" in load:
        return False
    return True


def _badge_ok(form):
    refresh = _method(form, "private async Task RefreshHub()")
    set_badge = _method(form, "private void SetBuildBadge(")
    index = _method(form, "private int IndexOf(")
    make = _method(form, "private Button MakeTab(")
    if "await RefreshBuildBadge()" not in refresh:
        return False
    if "if (!up)" not in refresh:
        return False
    if "SetBuildBadge(0)" not in refresh:
        return False
    if "WaitingJobCount" in form:
        return False
    if 'buildTab.Text = "Build"' not in set_badge:
        return False
    if "Build (" in set_badge:
        return False
    if '(tabButtons[i].Tag as string) == title' not in index:
        return False
    if "Tag = title" not in make:
        return False
    return True


class TestBuildApprovals(unittest.TestCase):
    def test_gitignore_peb_read_check_bin_obj(self):
        self.assertIn("tests/peb_read_check/obj/", GITIGNORE)
        self.assertIn("tests/peb_read_check/bin/", GITIGNORE)

    def test_panel_lists_held_jobs_and_needs_you(self):
        self.assertNotIn('id="approvals"', BUILD)
        self.assertNotIn("loadApprovals", BUILD)
        self.assertNotIn("NEEDS-YOU", BUILD)
        self.assertNotIn("Held jobs", BUILD)
        self.assertNotIn("function decide", BUILD)
        self.assertNotIn("/api/decide", BUILD)

    def test_queue_exposes_from_lane_screen_safe_summary(self):
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        bus = home / "bus"
        bus.mkdir()
        for name in (
            "LEAD-TO-BUILD.md",
            "GROKBOT-TO-BUILD.md",
            "BUILD-TO-LEAD.md",
            "BUILD-TO-GROKBOT.md",
        ):
            (bus / name).write_text("", encoding="utf-8")
        leak = r"list C:\Users\agentid\secret and then stop"
        (bus / "LEAD-TO-BUILD.md").write_text(
            "id: hold-panel-1\nv: 2\nfrom: Lead\nto: Build\nlane: ops\n"
            "priority: normal\ntime: 2026-10-11T12:00:00-04:00\n"
            "routed: Lead 2026-10-11T12:00:00-04:00\n"
            "job: %s\ndone-when: files listed\n" % leak,
            encoding="utf-8",
        )
        prev = {
            "BUS": server.BUS,
            "LOG": server.LOG,
            "SEEN": server.SEEN,
            "SESSIONS": server.SESSIONS,
        }
        prev_events = getattr(server, "EVENTS", None)
        hub = point_hub_at(home)
        try:
            server.BUS = bus
            server.LOG = home / "approvals.jsonl"
            server.SEEN = home / ".seen-ids.txt"
            server.SESSIONS = home / ".sessions.json"
            if prev_events is not None:
                server.EVENTS = home / ".hub-events.jsonl"
            items = [row for row in server.queue() if row.get("id") == "hold-panel-1"]
            self.assertEqual(len(items), 1)
            row = items[0]
            self.assertEqual(row.get("from"), server.screen_safe("Lead"))
            self.assertEqual(row.get("lane"), server.screen_safe("ops"))
            self.assertEqual(row.get("status"), "held")
            self.assertEqual(row.get("summary"), server.screen_safe(leak))
            self.assertTrue(row.get("hash"))
            self.assertEqual(len(row.get("hash")), 64)
            self.assertNotIn("agentid", row.get("summary") or "")
            self.assertNotIn(r"C:\Users", row.get("summary") or "")
            self.assertTrue(hasattr(server, "screen_safe"))
            self.assertEqual(server.screen_safe("a" * 200), ("a" * 160) + "...")
        finally:
            server.BUS = prev["BUS"]
            server.LOG = prev["LOG"]
            server.SEEN = prev["SEEN"]
            server.SESSIONS = prev["SESSIONS"]
            if prev_events is not None:
                server.EVENTS = prev_events
            restore_hub(hub)
            tmp.cleanup()

    def test_approve_and_deny_call_decide(self):
        self.assertNotIn("function decide", BUILD)
        self.assertNotIn("loadApprovals", BUILD)
        desk_decide = _js_fn(DESK, "decide")
        self.assertTrue(_decide_ok(desk_decide), desk_decide)

    def test_approvals_recover_after_hub_restart(self):
        self.assertNotIn("loadApprovals", BUILD)
        self.assertIn("fetch(\"/api/state\"", BUILD)
        self.assertIn("currentToken", BUILD)
        self.assertIn("RefreshBuildTokenAfterHubUp", FORM)
        refresh = _method(FORM, "private async Task RefreshHub()")
        self.assertIn("if (wasDown) await RefreshBuildTokenAfterHubUp()", refresh)
        recover = _method(FORM, "private async Task RefreshBuildTokenAfterHubUp()")
        self.assertIn("await SessionTokenAsync(force: true)", recover)
        self.assertIn("window.__hbcToken", recover)
        self.assertTrue(_hub_build_origin_ok(FORM), recover)
        no_origin = FORM.replace("if (!IsHubBuildPage(buildView.Source)) return;", "")
        self.assertFalse(_hub_build_origin_ok(no_origin))
        no_live = FORM.replace(
            "if (!Uri.TryCreate(core.Source, UriKind.Absolute, out var live) || !IsHubBuildPage(live)) return;",
            "",
        )
        self.assertFalse(_hub_build_origin_ok(no_live))
        localhost = FORM.replace('source.Host == "127.0.0.1"', 'source.Host == "localhost"')
        self.assertFalse(_hub_build_origin_ok(localhost))

    def test_decide_after_hub_restart_does_not_403(self):
        self.assertNotIn("function decide", BUILD)
        from aiohttp.test_utils import TestClient, TestServer

        prev_token = server.TOKEN
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        bus = home / "bus"
        bus.mkdir()
        for name in (
            "LEAD-TO-BUILD.md",
            "GROKBOT-TO-BUILD.md",
            "BUILD-TO-LEAD.md",
            "BUILD-TO-GROKBOT.md",
        ):
            (bus / name).write_text("", encoding="utf-8")
        prev = {"BUS": server.BUS, "LOG": server.LOG, "SEEN": server.SEEN, "SESSIONS": server.SESSIONS}
        prev_events = getattr(server, "EVENTS", None)
        hub = point_hub_at(home)
        old_token = "a" * 32
        new_token = "b" * 32
        server.TOKEN = old_token
        payload = {"id": "any", "decision": "approve", "hash": "0" * 64}
        try:
            server.BUS = bus
            server.LOG = home / "approvals.jsonl"
            server.SEEN = home / ".seen-ids.txt"
            server.SESSIONS = home / ".sessions.json"
            if prev_events is not None:
                server.EVENTS = home / ".hub-events.jsonl"

            async def run():
                app = server.make_app()
                async with TestClient(TestServer(app)) as client:
                    before = await client.post(
                        "/api/decide",
                        headers={"Host": HUB_HOST, "X-Hbc-Token": old_token},
                        json=payload,
                    )
                    server.TOKEN = new_token
                    stale_resp = await client.post(
                        "/api/decide",
                        headers={"Host": HUB_HOST, "X-Hbc-Token": old_token},
                        json=payload,
                    )
                    approve = await client.post(
                        "/api/decide",
                        headers={"Host": HUB_HOST, "X-Hbc-Token": new_token},
                        json=payload,
                    )
                    deny = await client.post(
                        "/api/decide",
                        headers={"Host": HUB_HOST, "X-Hbc-Token": new_token},
                        json={"id": "any", "decision": "deny", "hash": "0" * 64},
                    )
                    return (
                        before.status,
                        stale_resp.status,
                        approve.status,
                        deny.status,
                    )

            before_status, stale_status, approve_status, deny_status = asyncio.run(run())
            self.assertNotEqual(before_status, 403)
            self.assertEqual(stale_status, 403)
            self.assertNotEqual(approve_status, 403)
            self.assertNotEqual(deny_status, 403)
        finally:
            server.TOKEN = prev_token
            server.BUS = prev["BUS"]
            server.LOG = prev["LOG"]
            server.SEEN = prev["SEEN"]
            server.SESSIONS = prev["SESSIONS"]
            if prev_events is not None:
                server.EVENTS = prev_events
            restore_hub(hub)
            tmp.cleanup()

    def test_badge_count_updates_from_ready_queue(self):
        self.assertTrue(_badge_ok(FORM), "badge path incomplete")
        refresh = _method(FORM, "private async Task RefreshHub()")
        self.assertIn("await RefreshBuildBadge()", refresh)
        self.assertIn("hubTimer.Tick += OnHubTick", FORM)
        self.assertIn("if (hubTickBusy) return", FORM)
        self.assertIn('buildTab = MakeTab("Build")', FORM)
        self.assertNotIn("WaitingJobCount", FORM)
        set_badge = _method(FORM, "private void SetBuildBadge(")
        self.assertIn('buildTab.Text = "Build"', set_badge)
        self.assertNotIn("Build (", set_badge)

    def test_no_token_in_state_or_decide_urls(self):
        for label, source in (("build.html", BUILD), ("desk.js", DESK), ("Form1.cs", FORM)):
            for _quote, url in _state_fetches(source):
                self.assertEqual(url, "/api/state", label)
                self.assertNotIn("token=", url)
            for _quote, url in _decide_fetches(source):
                self.assertEqual(url, "/api/decide", label)
                self.assertNotIn("token=", url)
        badge = _method(FORM, "private Task RefreshBuildBadge()")
        send = _method(FORM, "private static async Task<HttpResponseMessage> SendHubState(")
        self.assertIn('HubBase + "/api/state"', send)
        self.assertIn("new Uri(HubBase).Authority", send)
        self.assertNotIn("/api/state", badge)
        self.assertNotIn("/api/decide", badge)
        self.assertNotIn("api/state?token=", send)
        self.assertNotIn("api/decide?token=", BUILD)
        self.assertNotIn("api/state?token=", BUILD)
        self.assertNotIn("api/state?token=", DESK)
        self.assertIn("/term/", BUILD)
        self.assertIn("?token=", BUILD)

    def test_decide_call_mutants(self):
        self.assertNotIn("function decide", BUILD)
        decide = _js_fn(DESK, "decide")
        self.assertTrue(_decide_ok(decide))
        mutants = (
            (decide.replace('fetch("/api/decide"', 'fetch("/api/decide?token=" + token + "'), "token in decide URL"),
            (decide.replace('method: "POST"', 'method: "GET"'), "GET decide"),
            (decide.replace('"X-Hbc-Token": window.HBC_TOKEN || ""', '"X-Token": window.HBC_TOKEN || ""'), "missing X-Hbc-Token"),
            (decide.replace("JSON.stringify({ id, decision, hash })", "JSON.stringify({ id, decision })"), "hash dropped"),
            (decide.replace("JSON.stringify({ id, decision, hash })", "JSON.stringify({ id, decision, lane: job.lane })"), "lane on decide"),
            (decide.replace('fetch("/api/decide"', 'fetch("/api/open"'), "wrong path"),
            (decide.replace("setDecideButtons(false)", "setDecideButtons(true)"), "buttons stay enabled"),
            (decide.replace("if (decideBusy) return;", ""), "decideBusy guard dropped"),
            (decide.replace("decideBusy = true", "decideBusy = false"), "decideBusy not set"),
        )
        for mutant, why in mutants:
            if _decide_ok(mutant):
                self.fail("decide mutant survived: %s" % why)

    def test_badge_mutants(self):
        self.assertTrue(_badge_ok(FORM))
        mutants = (
            (FORM.replace("await RefreshBuildBadge();", "return;"), "no badge refresh"),
            (FORM.replace('buildTab.Text = "Build";', 'buildTab.Text = waiting > 0 ? "Build (" + waiting + ")" : "Build";'), "count returns"),
            (FORM.replace("(tabButtons[i].Tag as string) == title", "tabButtons[i].Text == title"), "IndexOf uses badge text"),
        )
        for mutant, why in mutants:
            if _badge_ok(mutant):
                self.fail("badge mutant survived: %s" % why)

    def test_state_get_has_no_decide_side_effect(self):
        live_root = Path(server.__file__).resolve().parent
        live_seen = live_root / ".seen-ids.txt"
        live_init = live_root / ".seen-ids.txt.init"
        seen_stat = live_seen.stat() if live_seen.is_file() else None
        init_existed = live_init.exists()
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        bus = home / "bus"
        bus.mkdir()
        for name in (
            "LEAD-TO-BUILD.md",
            "GROKBOT-TO-BUILD.md",
            "BUILD-TO-LEAD.md",
            "BUILD-TO-GROKBOT.md",
        ):
            (bus / name).write_text("", encoding="utf-8")
        prev = {
            "BUS": server.BUS,
            "LOG": server.LOG,
            "SEEN": server.SEEN,
            "SESSIONS": server.SESSIONS,
        }
        prev_events = getattr(server, "EVENTS", None)
        hub = point_hub_at(home)
        try:
            server.BUS = bus
            server.LOG = home / "approvals.jsonl"
            server.SEEN = home / ".seen-ids.txt"
            server.SESSIONS = home / ".sessions.json"
            if prev_events is not None:
                server.EVENTS = home / ".hub-events.jsonl"
            with mock.patch.object(server, "apply_decision") as dec:
                payload = server.state_payload()
                dec.assert_not_called()
            self.assertIn("queue", payload)
            self.assertIn("needs_you", payload)
        finally:
            server.BUS = prev["BUS"]
            server.LOG = prev["LOG"]
            server.SEEN = prev["SEEN"]
            server.SESSIONS = prev["SESSIONS"]
            if prev_events is not None:
                server.EVENTS = prev_events
            restore_hub(hub)
            tmp.cleanup()
        if seen_stat is None:
            self.assertFalse(live_seen.is_file())
        else:
            self.assertEqual(live_seen.stat().st_mtime_ns, seen_stat.st_mtime_ns)
        self.assertEqual(live_init.exists(), init_existed)
        badge = _method(FORM, "private async Task RefreshBuildBadge()")
        self.assertNotIn("apply_decision", badge)
        self.assertNotIn("/api/decide", badge)
        refresh = _method(FORM, "private async Task RefreshHub()")
        self.assertNotIn("/api/decide", refresh)

    def test_start_grok_confirm_unchanged(self):
        confirm = _method(FORM, "private async Task ConfirmStartGrok()")
        self.assertEqual(confirm, "")

    def test_state_http_uses_header_and_host(self):
        from aiohttp.test_utils import TestClient, TestServer

        prev_token = server.TOKEN
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        bus = home / "bus"
        bus.mkdir()
        for name in (
            "LEAD-TO-BUILD.md",
            "GROKBOT-TO-BUILD.md",
            "BUILD-TO-LEAD.md",
            "BUILD-TO-GROKBOT.md",
        ):
            (bus / name).write_text("", encoding="utf-8")
        (bus / "LEAD-TO-BUILD.md").write_text(
            "id: hold-http-1\nv: 2\nfrom: Lead\nto: Build\nlane: lab\n"
            "priority: normal\ntime: 2026-10-11T12:00:00-04:00\n"
            "routed: Lead 2026-10-11T12:00:00-04:00\n"
            "job: ping the lab pane\ndone-when: pinged\n",
            encoding="utf-8",
        )
        prev = {
            "BUS": server.BUS,
            "LOG": server.LOG,
            "SEEN": server.SEEN,
            "SESSIONS": server.SESSIONS,
        }
        prev_events = getattr(server, "EVENTS", None)
        hub = point_hub_at(home)
        server.TOKEN = "a" * 32
        try:
            server.BUS = bus
            server.LOG = home / "approvals.jsonl"
            server.SEEN = home / ".seen-ids.txt"
            server.SESSIONS = home / ".sessions.json"
            if prev_events is not None:
                server.EVENTS = home / ".hub-events.jsonl"

            async def run():
                app = server.make_app()
                async with TestClient(TestServer(app)) as client:
                    bad = await client.get("/api/state", headers={"Host": HUB_HOST})
                    self.assertEqual(bad.status, 403)
                    good = await client.get(
                        "/api/state",
                        headers={"Host": HUB_HOST, "X-Hbc-Token": server.TOKEN},
                    )
                    host_miss = await client.get(
                        "/api/state",
                        headers={"Host": "127.0.0.1:9999", "X-Hbc-Token": server.TOKEN},
                    )
                    decide_get = await client.get(
                        "/api/decide",
                        headers={"Host": HUB_HOST, "X-Hbc-Token": server.TOKEN},
                    )
                    missing = await client.post(
                        "/api/decide",
                        headers={"Host": HUB_HOST, "X-Hbc-Token": server.TOKEN},
                        json={"id": "hold-http-1", "decision": "deny"},
                    )
                    payload = await good.json()
                    rows = [row for row in payload.get("queue", []) if row.get("id") == "hold-http-1"]
                    job_hash = rows[0].get("hash") if rows else ""
                    stale = await client.post(
                        "/api/decide",
                        headers={"Host": HUB_HOST, "X-Hbc-Token": server.TOKEN},
                        json={"id": "hold-http-1", "decision": "approve", "hash": "0" * 64},
                    )
                    deny = await client.post(
                        "/api/decide",
                        headers={"Host": HUB_HOST, "X-Hbc-Token": server.TOKEN},
                        json={"id": "hold-http-1", "decision": "deny", "hash": job_hash},
                    )
                    approve = await client.post(
                        "/api/decide",
                        headers={"Host": HUB_HOST, "X-Hbc-Token": server.TOKEN},
                        json={"id": "hold-http-1", "decision": "approve", "hash": job_hash},
                    )
                    return (
                        good.status,
                        payload,
                        host_miss.status,
                        decide_get.status,
                        missing.status,
                        await missing.json(),
                        stale.status,
                        await stale.json(),
                        deny.status,
                        await deny.json(),
                        approve.status,
                        await approve.json(),
                    )

            (
                status, payload, host_status, get_decide,
                missing_status, missing_body, stale_status, stale_body,
                deny_status, deny_body, approve_status, approve_body,
            ) = asyncio.run(run())
            self.assertEqual(status, 200)
            rows = [row for row in payload.get("queue", []) if row.get("id") == "hold-http-1"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].get("from"), "Lead")
            self.assertEqual(rows[0].get("lane"), "lab")
            self.assertEqual(rows[0].get("summary"), server.screen_safe("ping the lab pane"))
            self.assertTrue(rows[0].get("hash"))
            self.assertEqual(host_status, 421)
            self.assertEqual(get_decide, 405)
            self.assertEqual(missing_status, 400)
            self.assertEqual(missing_body.get("error"), "HASH_REQUIRED")
            self.assertEqual(stale_status, 409)
            self.assertEqual(stale_body.get("error"), "STALE")
            self.assertEqual(deny_status, 403)
            self.assertIn("error", deny_body)
            self.assertNotEqual(deny_body.get("error"), "STALE")
            self.assertEqual(approve_status, 403)
            self.assertIn("error", approve_body)
            self.assertEqual(payload.get("needs_you"), [])
        finally:
            server.TOKEN = prev_token
            server.BUS = prev["BUS"]
            server.LOG = prev["LOG"]
            server.SEEN = prev["SEEN"]
            server.SESSIONS = prev["SESSIONS"]
            if prev_events is not None:
                server.EVENTS = prev_events
            restore_hub(hub)
            tmp.cleanup()

    def test_id_from_summary_not_innerhtml(self):
        self.assertNotIn("loadApprovals", BUILD)
        self.assertTrue(_job_fields_textcontent(DESK))
        desk = _js_fn(DESK, "load")
        desk_mutant = desk.replace("title.textContent =", "title.innerHTML =")
        if _job_fields_textcontent(desk_mutant):
            self.fail("desk.js innerHTML render of id survived")

    def test_desk_header_token_mutant(self):
        self.assertTrue(_desk_header_ok(DESK))
        dropped = DESK.replace('headers: { "X-Hbc-Token": token },', "headers: {},")
        if _desk_header_ok(dropped):
            self.fail("desk.js header token drop survived")
        query = DESK.replace(
            'fetch("/api/state", {\n    headers: { "X-Hbc-Token": token },\n  })',
            'fetch("/api/state?token=" + encodeURIComponent(token))',
        )
        if _desk_header_ok(query):
            self.fail("desk.js token-in-URL survived")

    def test_screen_safe_strips_rlo_zw_ansi(self):
        rlo = "ok\u202eevil"
        self.assertEqual(server.screen_safe(rlo), "okevil")
        self.assertNotIn("\u202e", server.screen_safe(rlo))
        zw = "ok\u200bhid\u200cden\u2060\ufeff"
        self.assertEqual(server.screen_safe(zw), "okhidden")
        ansi = "ok\x1b[31mred\x1b[0m"
        self.assertEqual(server.screen_safe(ansi), "okred")
        self.assertNotIn("\x1b", server.screen_safe(ansi))
        mixed = "id-\u202a1\u202c"
        self.assertEqual(server.screen_safe(mixed), "id-1")
        self.assertEqual(server.screen_safe("ok\u2066hid\u2069"), "okhid")
        self.assertEqual(server.screen_safe("ok\u200e\u200f"), "ok")
        self.assertEqual(server.screen_safe("ok\x07bell"), "okbell")
        self.assertEqual(server.screen_safe("ok\x9bhid"), "okhid")
        self.assertEqual(server.screen_safe("ok\u061chid"), "okhid")

    def test_queue_sanitizes_id_from_lane(self):
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        bus = home / "bus"
        bus.mkdir()
        for name in (
            "LEAD-TO-BUILD.md",
            "GROKBOT-TO-BUILD.md",
            "BUILD-TO-LEAD.md",
            "BUILD-TO-GROKBOT.md",
        ):
            (bus / name).write_text("", encoding="utf-8")
        (bus / "LEAD-TO-BUILD.md").write_text(
            "id: vis-1\u202e\nv: 2\nfrom: Le\u200bad\nto: Build\nlane: op\x1b[31ms\n"
            "priority: normal\ntime: 2026-10-11T12:00:00-04:00\n"
            "routed: Lead 2026-10-11T12:00:00-04:00\n"
            "job: ping\ndone-when: pinged\n",
            encoding="utf-8",
        )
        prev = {"BUS": server.BUS, "LOG": server.LOG, "SEEN": server.SEEN, "SESSIONS": server.SESSIONS}
        prev_events = getattr(server, "EVENTS", None)
        hub = point_hub_at(home)
        try:
            server.BUS = bus
            server.LOG = home / "approvals.jsonl"
            server.SEEN = home / ".seen-ids.txt"
            server.SESSIONS = home / ".sessions.json"
            if prev_events is not None:
                server.EVENTS = home / ".hub-events.jsonl"
            items = server.queue()
            self.assertTrue(items)
            row = items[0]
            self.assertEqual(row.get("id"), "vis-1")
            self.assertEqual(row.get("from"), "Lead")
            self.assertEqual(row.get("lane"), "ops")
            self.assertEqual(row.get("status"), "held")
            self.assertEqual(row.get("reason"), "BAD_ID")
            self.assertNotIn("\u202e", row.get("id") or "")
            self.assertNotIn("\u200b", row.get("from") or "")
            self.assertNotIn("\x1b", row.get("lane") or "")
            self.assertFalse(server._job_id_matches({"id": "vis-1\u202e"}, "vis-1"))
            self.assertIsNone(server.find_job("vis-1")[0])
        finally:
            server.BUS = prev["BUS"]
            server.LOG = prev["LOG"]
            server.SEEN = prev["SEEN"]
            server.SESSIONS = prev["SESSIONS"]
            if prev_events is not None:
                server.EVENTS = prev_events
            restore_hub(hub)
            tmp.cleanup()

    def test_stale_view_hash_refused(self):
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        bus = home / "bus"
        bus.mkdir()
        for name in (
            "LEAD-TO-BUILD.md",
            "GROKBOT-TO-BUILD.md",
            "BUILD-TO-LEAD.md",
            "BUILD-TO-GROKBOT.md",
        ):
            (bus / name).write_text("", encoding="utf-8")
        (bus / "LEAD-TO-BUILD.md").write_text(
            "id: stale-1\nv: 2\nfrom: Lead\nto: Build\nlane: lab\n"
            "priority: normal\ntime: 2026-10-11T12:00:00-04:00\n"
            "routed: Lead 2026-10-11T12:00:00-04:00\n"
            "job: ping\ndone-when: pinged\n",
            encoding="utf-8",
        )
        prev = {"BUS": server.BUS, "LOG": server.LOG, "SEEN": server.SEEN, "SESSIONS": server.SESSIONS}
        prev_events = getattr(server, "EVENTS", None)
        hub = point_hub_at(home)
        try:
            server.BUS = bus
            server.LOG = home / "approvals.jsonl"
            server.SEEN = home / ".seen-ids.txt"
            server.SESSIONS = home / ".sessions.json"
            if prev_events is not None:
                server.EVENTS = home / ".hub-events.jsonl"
            items = [row for row in server.queue() if row.get("id") == "stale-1"]
            self.assertEqual(len(items), 1)
            status, payload = server.apply_decision("stale-1", "approve", view_hash="0" * 64)
            self.assertEqual(status, 409)
            self.assertEqual(payload.get("error"), "STALE")
            status, payload = server.apply_decision(
                "stale-1", "approve", view_hash=items[0]["hash"]
            )
            self.assertNotEqual(payload.get("error"), "STALE")
        finally:
            server.BUS = prev["BUS"]
            server.LOG = prev["LOG"]
            server.SEEN = prev["SEEN"]
            server.SESSIONS = prev["SESSIONS"]
            if prev_events is not None:
                server.EVENTS = prev_events
            restore_hub(hub)
            tmp.cleanup()

    def test_job_id_matches_has_no_scrubbed_fallback(self):
        src = inspect.getsource(server._job_id_matches)
        if "screen_safe" in src:
            self.fail("scrubbed-id fallback returned")
        self.assertTrue(server._job_id_matches({"id": "vis-1"}, "vis-1"))
        self.assertFalse(server._job_id_matches({"id": "vis-1\u202e"}, "vis-1"))
        self.assertFalse(server._job_id_matches({"id": "vis-1\u200b"}, "vis-1"))
        self.assertEqual(server.id_reason({"id": "vis-1\u202e"}), "BAD_ID")
        self.assertEqual(server.id_reason({"id": "vis-1\x9b"}), "BAD_ID")
        self.assertEqual(server.id_reason({"id": "vis-1\u061c"}), "BAD_ID")
        self.assertEqual(server.id_reason({"id": "vis-1"}), "")
        mutant = src.replace(
            "return fields.get(\"id\") == job_id",
            "return fields.get(\"id\") == job_id or bool(job_id) and "
            "screen_safe(fields.get(\"id\") or \"\") == job_id",
        )
        if "screen_safe" not in mutant:
            self.fail("fallback mutant did not insert screen_safe")
        ns = {"screen_safe": server.screen_safe}
        exec(compile(mutant, "<scrubfb>", "exec"), ns)
        mutated = ns["_job_id_matches"]
        self.assertTrue(mutated({"id": "vis-1"}, "vis-1"))
        self.assertTrue(mutated({"id": "vis-1\u202e"}, "vis-1"))
        self.assertFalse(server._job_id_matches({"id": "vis-1\u202e"}, "vis-1"))
        if "screen_safe" in inspect.getsource(server._job_id_matches):
            self.fail("scrubbed-id fallback survived")

    def test_decide_busy_guard_mutant(self):
        self.assertNotIn("function decide", BUILD)
        decide = _js_fn(DESK, "decide")
        self.assertIn("if (decideBusy) return", decide)
        dropped = decide.replace("if (decideBusy) return;", "")
        if _decide_ok(dropped):
            self.fail("desk.js decideBusy guard drop survived")

    def test_decide_rejects_sig_alias(self):
        self.assertNotIn('body.get("sig")', inspect.getsource(server.decide))
        self.assertNotIn('body.get("sig")', inspect.getsource(server.Handler_retired.decide))
        from aiohttp.test_utils import TestClient, TestServer

        prev_token = server.TOKEN
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        bus = home / "bus"
        bus.mkdir()
        for name in (
            "LEAD-TO-BUILD.md",
            "GROKBOT-TO-BUILD.md",
            "BUILD-TO-LEAD.md",
            "BUILD-TO-GROKBOT.md",
        ):
            (bus / name).write_text("", encoding="utf-8")
        prev = {"BUS": server.BUS, "LOG": server.LOG, "SEEN": server.SEEN, "SESSIONS": server.SESSIONS}
        prev_events = getattr(server, "EVENTS", None)
        hub = point_hub_at(home)
        server.TOKEN = "a" * 32
        try:
            server.BUS = bus
            server.LOG = home / "approvals.jsonl"
            server.SEEN = home / ".seen-ids.txt"
            server.SESSIONS = home / ".sessions.json"
            if prev_events is not None:
                server.EVENTS = home / ".hub-events.jsonl"

            async def run():
                app = server.make_app()
                async with TestClient(TestServer(app)) as client:
                    resp = await client.post(
                        "/api/decide",
                        headers={"Host": HUB_HOST, "X-Hbc-Token": server.TOKEN},
                        json={"id": "any", "decision": "approve", "sig": "0" * 64},
                    )
                    return resp.status, await resp.json()

            status, body = asyncio.run(run())
            self.assertEqual(status, 400)
            self.assertEqual(body.get("error"), "HASH_REQUIRED")
        finally:
            server.TOKEN = prev_token
            server.BUS = prev["BUS"]
            server.LOG = prev["LOG"]
            server.SEEN = prev["SEEN"]
            server.SESSIONS = prev["SESSIONS"]
            if prev_events is not None:
                server.EVENTS = prev_events
            restore_hub(hub)
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
