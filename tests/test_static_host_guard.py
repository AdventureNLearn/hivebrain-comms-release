"""Static allowlist and Host: 127.0.0.1:8107 gate. Secrets stay in tmp ROOT."""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server
from tests.live_guard import install_live_state_guard, point_hub_at, restore_hub
from tests.runtime_skip import skip_symlink_denied

install_live_state_guard()

HUB_HOST = "127.0.0.1:8107"
ALLOWED = ("index.html", "build.html", "desk.js", "desk.css")
PLANTED_TOKEN = "planted-session-token-not-for-http"
PLANTED_SESSIONS = '{"sessions":["secret-session"]}\n'
PLANTED_SERVER = "SECRET_SERVER_SOURCE = 'do-not-serve'\n"
PLANTED_HIDDEN = "hidden-dotfile\n"
PLANTED_PS1 = "Write-Host secret-ps1\n"
PLANTED_BAK = "backup-should-not-serve\n"
PLANTED_EXTRA = "<html>extra-not-allowlisted</html>\n"


class _Req:
    def __init__(self, path, raw_path=None):
        self.path = path
        self.raw_path = raw_path if raw_path is not None else path


def _http(method, path, host=HUB_HOST, headers=None):
    async def run():
        app = server.make_app()
        async with TestClient(TestServer(app)) as client:
            hdrs = {"Host": host}
            if headers:
                hdrs.update(headers)
            resp = await getattr(client, method)(path, headers=hdrs)
            return resp.status, await resp.read()

    return asyncio.run(run())


class TestStaticHostGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        for name in ALLOWED:
            shutil.copy(ROOT / name, self.home / name)
        (self.home / ".session-token").write_text(PLANTED_TOKEN + "\n", encoding="utf-8")
        (self.home / ".sessions.json").write_text(PLANTED_SESSIONS, encoding="utf-8")
        (self.home / "server.py").write_text(PLANTED_SERVER, encoding="utf-8")
        (self.home / ".hidden").write_text(PLANTED_HIDDEN, encoding="utf-8")
        (self.home / "foo.ps1").write_text(PLANTED_PS1, encoding="utf-8")
        (self.home / "desk.js.bak").write_text(PLANTED_BAK, encoding="utf-8")
        (self.home / "extra.html").write_text(PLANTED_EXTRA, encoding="utf-8")
        self.prev_hub = point_hub_at(self.home)
        self.prev_token = server.TOKEN
        self.prev_port = server.PORT
        self.prev_http_host = server.HUB_HTTP_HOST
        server.apply_hub_port(str(server.DEFAULT_HUB_PORT))
        server.TOKEN = "a" * 32

    def tearDown(self):
        server.TOKEN = self.prev_token
        server.PORT = self.prev_port
        server.HUB_HTTP_HOST = self.prev_http_host
        restore_hub(self.prev_hub)
        self.tmp.cleanup()

    def _assert_secret_absent(self, body):
        text = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")
        self.assertNotIn(PLANTED_TOKEN.encode("ascii"), text)
        self.assertNotIn(b"secret-session", text)
        self.assertNotIn(b"SECRET_SERVER_SOURCE", text)
        self.assertNotIn(b"hidden-dotfile", text)
        self.assertNotIn(b"secret-ps1", text)
        self.assertNotIn(PLANTED_BAK.strip().encode("ascii"), text)
        self.assertNotIn(b"extra-not-allowlisted", text)

    def test_01_refuses_token_sessions_server_dotfiles_ps1(self):
        for path in (
            "/.session-token",
            "/.sessions.json",
            "/server.py",
            "/.hidden",
            "/foo.ps1",
            "/desk.js.bak",
            "/extra.html",
        ):
            status, body = _http("get", path)
            self.assertEqual(status, 404, path)
            self.assertEqual(body, b"no", path)
            self._assert_secret_absent(body)

        self.assertIsNone(server.requested_static_name(_Req("/.session-token")))
        self.assertIsNone(server.requested_static_name(_Req("/.sessions.json")))
        self.assertIsNone(server.requested_static_name(_Req("/server.py")))
        self.assertIsNone(server.requested_static_name(_Req("/.hidden")))
        self.assertIsNone(server.requested_static_name(_Req("/foo.ps1")))
        self.assertIsNone(server.resolve_static_page("server.py", root=self.home))
        self.assertIsNone(server.resolve_static_page(".session-token", root=self.home))

    def test_02_evil_host_refused_on_index_api_term(self):
        evil = (
            "evil.example:8107",
            "localhost:8107",
            "127.0.0.1",
            "127.0.0.1:81070",
            "127.0.0.1:8107.evil",
            "127.0.0.1:9999",
            "[::1]:8107",
            "",
        )
        for host in evil:
            for path in ("/", "/api/state", "/term/1", "/term/build"):
                status, body = _http("get", path, host=host)
                self.assertEqual(status, 421, "%s %s" % (host, path))
                self.assertEqual(body, b"refused", "%s %s" % (host, path))
                self._assert_secret_absent(body)
                self.assertNotIn(server.TOKEN.encode("ascii"), body)

    def test_03_allowed_pages_still_load(self):
        for path, needle, ctype in (
            ("/", b"HiveBrainComms", "text/html"),
            ("/index.html", b"HiveBrainComms", "text/html"),
            ("/build.html", b"xterm", "text/html"),
            ("/desk.js", b"", "text/javascript"),
            ("/desk.css", b"", "text/css"),
        ):
            async def run(path=path):
                app = server.make_app()
                async with TestClient(TestServer(app)) as client:
                    resp = await client.get(path, headers={"Host": HUB_HOST})
                    body = await resp.read()
                    return resp.status, body, resp.content_type

            status, body, got_type = asyncio.run(run())
            self.assertEqual(status, 200, path)
            self.assertIn(ctype, got_type, path)
            if needle:
                self.assertIn(needle, body, path)
            self._assert_secret_absent(body)
            if path in ("/", "/index.html") and b"{{TOKEN}}" in (ROOT / "index.html").read_bytes():
                self.assertIn(server.TOKEN.encode("ascii"), body)
                self.assertNotIn(b"{{TOKEN}}", body)

        self.assertEqual(server.requested_static_name(_Req("/")), "index.html")
        self.assertEqual(server.requested_static_name(_Req("/desk.js")), "desk.js")
        self.assertIsNotNone(server.resolve_static_page("index.html", root=self.home))
        self.assertIsNotNone(server.resolve_static_page("desk.css", root=self.home))

    def test_04_allowlist_mutants(self):
        self.assertEqual(server.STATIC_PAGE_NAMES, frozenset(ALLOWED))
        # startswith(allowlisted) would accept desk.js.bak / index.html.evil
        self.assertIsNone(server.requested_static_name(_Req("/desk.js.bak")))
        self.assertIsNone(server.requested_static_name(_Req("/index.html.evil")))
        self.assertIsNone(server.requested_static_name(_Req("/build.html.bak")))
        self.assertIsNone(server.requested_static_name(_Req("/desk.css.bak")))
        # suffix / "in name" would accept evildesk.js or extra.html
        self.assertIsNone(server.requested_static_name(_Req("/evildesk.js")))
        self.assertIsNone(server.requested_static_name(_Req("/extra.html")))
        self.assertIsNone(server.requested_static_name(_Req("/server.py")))
        # only exact membership
        for name in ALLOWED:
            self.assertEqual(server.requested_static_name(_Req("/" + name)), name)
            self.assertFalse(any(name.startswith(other) and name != other for other in ALLOWED))

        status, body = _http("get", "/desk.js.bak")
        self.assertEqual(status, 404)
        self.assertNotIn(b"backup-should-not-serve", body)
        status, body = _http("get", "/extra.html")
        self.assertEqual(status, 404)
        self.assertNotIn(b"extra-not-allowlisted", body)

        src = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn("STATIC_PAGE_NAMES", src)
        self.assertIn("name not in STATIC_PAGE_NAMES", src)
        self.assertNotRegex(
            src,
            r"requested_static_name[\s\S]*any\(\s*allowed in name",
        )

    def test_05_host_check_mutants(self):
        self.assertEqual(server.HUB_HTTP_HOST, HUB_HOST)
        self.assertTrue(server.hub_host_ok(HUB_HOST))
        # startswith would accept the longer port / suffix
        self.assertFalse(server.hub_host_ok("127.0.0.1:81070"))
        self.assertFalse(server.hub_host_ok("127.0.0.1:8107.evil"))
        self.assertFalse(server.hub_host_ok("127.0.0.1:8107 "))
        self.assertFalse(server.hub_host_ok(" 127.0.0.1:8107"))
        self.assertFalse(server.hub_host_ok("127.0.0.1:8107\r"))
        # claim_host_ok used to accept localhost/::1; those get HTTP 421
        port = str(server.PORT)
        self.assertFalse(server.claim_host_ok("localhost:%s" % port))
        self.assertFalse(server.hub_host_ok("localhost:%s" % port))
        self.assertFalse(server.claim_host_ok("127.0.0.1"))
        self.assertFalse(server.hub_host_ok("127.0.0.1"))
        self.assertFalse(server.claim_host_ok("[::1]:%s" % port))
        self.assertFalse(server.hub_host_ok("[::1]:%s" % port))
        self.assertFalse(server.claim_host_ok("LOCALHOST:%s" % port))
        self.assertFalse(server.hub_host_ok("LOCALHOST:%s" % port))
        self.assertTrue(server.claim_host_ok(HUB_HOST))

        names = [getattr(mw, "__name__", "") for mw in server.make_app().middlewares]
        self.assertEqual(names[0], "hub_host_middleware")

        src = Path(server.__file__).read_text(encoding="utf-8")
        host_fn = src.split("def hub_host_ok", 1)[1].split("def ", 1)[0]
        self.assertIn("== HUB_HTTP_HOST", host_fn)
        self.assertNotIn("startswith", host_fn)
        self.assertNotIn("claim_host_ok", host_fn)
        self.assertNotIn(".strip(", host_fn)
        self.assertNotIn(".lower(", host_fn)
        claim_fn = src.split("def claim_host_ok", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("localhost", claim_fn)
        self.assertNotIn("::1", claim_fn)

        status, body = _http("get", "/", host="127.0.0.1:81070")
        self.assertEqual(status, 421)
        self.assertEqual(body, b"refused")

    def test_06_encoded_backslash_traversal_refused(self):
        cases = (
            _Req("/.session-token", raw_path="/%2e%2e/.session-token"),
            _Req("/index.html", raw_path="/index.html%00"),
            _Req("/index.html", raw_path="/index.html%2f%2e%2e%2f.session-token"),
            _Req("/desk.js", raw_path=r"/desk.js\..\.\.session-token"),
            _Req(r"/foo\bar"),
            _Req("/../.session-token"),
            _Req("/..%2f.session-token"),
            _Req("/./index.html"),
            _Req("/index.html/../.session-token"),
            _Req("/bus/foo"),
        )
        for req in cases:
            self.assertIsNone(server.requested_static_name(req), req.path + " " + req.raw_path)

        status, body = _http("get", "/%2e%2e/.session-token")
        self.assertIn(status, (400, 404, 421))
        self._assert_secret_absent(body)
        status, body = _http("get", "/../.session-token")
        self.assertIn(status, (400, 404, 421))
        self._assert_secret_absent(body)

    def test_07_symlink_and_junction_refused(self):
        (self.home / "desk.js").unlink()
        planted = self.home / ".session-token"
        link = self.home / "desk.js"
        try:
            if os.name == "nt":
                target = self.home / "junc-target"
                target.mkdir()
                (target / "secret").write_text(PLANTED_TOKEN, encoding="utf-8")
                try:
                    import _winapi
                    _winapi.CreateJunction(str(target), str(link))
                except (AttributeError, OSError) as exc:
                    skip_symlink_denied(self, exc)
            else:
                os.symlink(planted, link)
        except OSError as exc:
            skip_symlink_denied(self, exc)
        from tests.runtime_skip import is_reparse_point
        self.assertTrue(link.is_symlink() or is_reparse_point(link))
        self.assertIsNone(server.resolve_static_page("desk.js", root=self.home))
        status, body = _http("get", "/desk.js")
        self.assertEqual(status, 404)
        self.assertNotIn(PLANTED_TOKEN.encode("ascii"), body)

        with patch.object(os.path, "isjunction", lambda p: Path(p).name == "index.html"):
            self.assertIsNone(server.resolve_static_page("index.html", root=self.home))
        self.assertIsNotNone(server.resolve_static_page("index.html", root=self.home))


if __name__ == "__main__":
    unittest.main(verbosity=2)
