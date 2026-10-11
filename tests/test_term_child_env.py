"""Interactive terminal child env allowlist. Mocks only. No secrets."""

import os
import re
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server
from tests.live_guard import install_live_state_guard
from hub.win_child_env import WIN_CHILD_ENV as SHARED_WIN_CHILD_ENV

install_live_state_guard()

ALLOWED = frozenset(("GROK_HOME", "PATH", "PROMPT") + server.WIN_CHILD_ENV + server.TERM_EXTRA_ENV)
AUTH_KEYS = frozenset(server.GROK_AUTH_KEYS)
PR2_WIN_CHILD_ENV = (
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
)


def _win_path(root="X:\\Windows"):
    return ";".join((
        str(server.GROK.parent),
        root + "\\System32",
        root + "\\System32\\Wbem",
        root + "\\System32\\WindowsPowerShell\\v1.0",
        root,
    ))

PARENT = {
    "SYSTEMROOT": "X:\\Windows",
    "WINDIR": "X:\\Windows",
    "COMSPEC": "X:\\Windows\\System32\\cmd.exe",
    "TEMP": "X:\\Temp",
    "TMP": "X:\\Temp",
    "USERPROFILE": "X:\\profile",
    "LOCALAPPDATA": "X:\\local",
    "APPDATA": "X:\\roaming",
    "PATHEXT": ".COM;.EXE",
    "USERNAME": "paneuser",
    "HOMEDRIVE": "X:",
    "HOMEPATH": "\\profile",
    "PROGRAMFILES": "X:\\Program Files",
    "ProgramData": "X:\\ProgramData",
    "GROK_HOME": "X:\\real-grok-home",
    "SECRET": "do-not-copy",
    "PATH": "X:\\evil",
    "HBC_TOKEN": "do-not-copy",
}


class TestTermChildEnv(unittest.TestCase):
    def test_secret_in_fake_environ_is_absent(self):
        home = Path("X") / "pane"
        env = server.term_child_env(home, pane="1", environ=PARENT)
        self.assertNotIn("SECRET", env)
        self.assertNotIn("HBC_TOKEN", env)
        self.assertNotIn("do-not-copy", env.values())

    def test_posix_non_build_sets_pane_home(self):
        home = Path("X") / "panes" / "pane-1"
        with mock.patch.object(os, "name", "posix"):
            env = server.term_child_env(home, pane="1", environ=PARENT)
        self.assertEqual(env["GROK_HOME"], str(home))
        self.assertEqual(env["PATH"], str(server.GROK.parent))
        self.assertEqual(env["PROMPT"], "pane1$G")
        self.assertNotIn("SECRET", env)
        self.assertNotIn("SYSTEMROOT", env)

    def test_build_keeps_real_grok_home(self):
        home = Path("X") / "panes" / "pane-build"
        with mock.patch.object(os, "name", "posix"):
            env = server.term_child_env(home, pane="build", environ=PARENT)
        self.assertEqual(env["GROK_HOME"], "X:\\real-grok-home")
        self.assertNotEqual(env["GROK_HOME"], str(home))
        extra = dict(PARENT)
        extra.pop("GROK_HOME")
        with mock.patch.object(os, "name", "posix"):
            env2 = server.term_child_env(home, pane="build2", environ=extra)
        self.assertEqual(env2["GROK_HOME"], extra["USERPROFILE"].rstrip("\\/") + "\\.grok")
        self.assertNotEqual(env2["GROK_HOME"], str(home))
        self.assertNotIn("SECRET", env2)
        self.assertNotIn("XAI_API_KEY", env2)

    def test_windows_allowlist_path_and_extras(self):
        home = Path("X") / "panes" / "pane-1"
        with mock.patch.object(os, "name", "nt"):
            env = server.term_child_env(home, pane="1", environ=PARENT)
        self.assertEqual(env["GROK_HOME"], str(home))
        self.assertEqual(env["PROMPT"], "pane1$G")
        self.assertEqual(env["SYSTEMROOT"], "X:\\Windows")
        self.assertEqual(env["USERNAME"], "paneuser")
        self.assertEqual(env["PATHEXT"], ".COM;.EXE")
        self.assertEqual(env["PATH"], _win_path())
        self.assertIn("\\System32\\Wbem", env["PATH"])
        self.assertNotIn("X:\\evil", env["PATH"])
        for key in server.WIN_CHILD_ENV + server.TERM_EXTRA_ENV:
            self.assertEqual(env.get(key), PARENT[key], key)
        self.assertTrue(set(env) <= ALLOWED)
        self.assertNotIn("SECRET", env)
        self.assertNotIn("HBC_TOKEN", env)

    def test_windows_skips_missing_keys(self):
        home = Path("X") / "pane"
        partial = {"SYSTEMROOT": "X:\\Windows", "SECRET": "do-not-copy"}
        with mock.patch.object(os, "name", "nt"):
            env = server.term_child_env(home, pane="1", environ=partial)
        self.assertEqual(env["SYSTEMROOT"], "X:\\Windows")
        self.assertNotIn("WINDIR", env)
        self.assertNotIn("USERNAME", env)
        self.assertNotIn("SECRET", env)
        self.assertTrue(set(env) <= ALLOWED)
        self.assertEqual(env["PATH"], _win_path())

    def test_win_child_env_is_the_shared_pr2_tuple(self):
        self.assertIs(server.WIN_CHILD_ENV, SHARED_WIN_CHILD_ENV)
        self.assertEqual(server.WIN_CHILD_ENV, PR2_WIN_CHILD_ENV)
        self.assertEqual(SHARED_WIN_CHILD_ENV, PR2_WIN_CHILD_ENV)

    def test_server_imports_win_child_env_and_does_not_define_it(self):
        src = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn("from hub.win_child_env import WIN_CHILD_ENV", src)
        self.assertIsNone(re.search(r"^WIN_CHILD_ENV\s*=", src, re.M))

    def test_pass_grok_auth_off_by_default(self):
        extra = dict(PARENT)
        extra["XAI_API_KEY"] = "xai-test-not-real"
        extra["GROK_API_KEY"] = "grok-test-not-real"
        home = Path("X") / "panes" / "pane-build"
        env = server.term_child_env(home, pane="build", environ=extra)
        self.assertNotIn("XAI_API_KEY", env)
        self.assertNotIn("GROK_API_KEY", env)
        extra["HBC_PASS_GROK_AUTH"] = "0"
        env0 = server.term_child_env(home, pane="build", environ=extra)
        self.assertNotIn("XAI_API_KEY", env0)
        extra["HBC_PASS_GROK_AUTH"] = "false"
        envf = server.term_child_env(home, pane="build", environ=extra)
        self.assertNotIn("XAI_API_KEY", envf)

    def test_pass_grok_auth_build_only(self):
        extra = dict(PARENT)
        extra["HBC_PASS_GROK_AUTH"] = "1"
        extra["XAI_API_KEY"] = "xai-test-not-real"
        extra["GROK_API_KEY"] = "grok-test-not-real"
        home = Path("X") / "panes" / "pane-build"
        env_build = server.term_child_env(home, pane="build", environ=extra)
        env_lab = server.term_child_env(home, pane="1", environ=extra)
        env_build2 = server.term_child_env(home, pane="build2", environ=extra)
        self.assertEqual(env_build["XAI_API_KEY"], "xai-test-not-real")
        self.assertEqual(env_build["GROK_API_KEY"], "grok-test-not-real")
        self.assertEqual(env_build2["XAI_API_KEY"], "xai-test-not-real")
        self.assertNotIn("XAI_API_KEY", env_lab)
        self.assertNotIn("GROK_API_KEY", env_lab)
        self.assertNotIn("SECRET", env_build)
        self.assertTrue(set(env_build) <= ALLOWED | AUTH_KEYS)

    @contextmanager
    def _windows_nt(self, files=None):
        mapped = {str(key).replace("/", "\\").casefold(): bool(value) for key, value in (files or {}).items()}
        real_isfile = os.path.isfile
        real_isabs = os.path.isabs

        def isfile(path):
            key = str(path).replace("/", "\\").casefold()
            if key in mapped:
                return mapped[key]
            return real_isfile(path)

        def isabs(path):
            text = str(path)
            if len(text) >= 2 and text[0].isalpha() and text[1] == ":":
                return True
            return real_isabs(path)

        with mock.patch.object(os, "name", "nt"), mock.patch.object(
            os.path, "isfile", side_effect=isfile
        ), mock.patch.object(os.path, "isabs", side_effect=isabs):
            yield

    def test_parent_path_tools_appended_not_copied(self):
        git_dir, grok_dir, wmic_dir = r"X:\git-bin", r"X:\grok-bin", r"X:\wmic-bin"
        decoy_dir, missing = r"X:\decoy-bin", r"X:\missing"
        parent = dict(PARENT)
        parent["PATH"] = ";".join((git_dir, grok_dir, wmic_dir, decoy_dir, missing))
        home = Path("X") / "panes" / "pane-1"
        files = {
            git_dir + r"\git.exe": True,
            grok_dir + r"\grok.exe": True,
            wmic_dir + r"\wmic.exe": True,
            decoy_dir + r"\secret.exe": True,
        }
        with self._windows_nt(files):
            env = server.term_child_env(home, pane="1", environ=parent)
            env_build = server.term_child_env(home, pane="build", environ=parent)
        for env_out in (env, env_build):
            parts = env_out["PATH"].split(";")
            self.assertIn("\\System32\\Wbem", env_out["PATH"])
            self.assertIn(git_dir, parts)
            self.assertIn(grok_dir, parts)
            self.assertIn(wmic_dir, parts)
            self.assertNotIn(decoy_dir, parts)
            self.assertNotIn("secret.exe", env_out["PATH"])
            self.assertNotIn(missing, env_out["PATH"])
            self.assertNotIn(git_dir + r"\git.exe", parts)
            self.assertNotIn(grok_dir + r"\grok.exe", parts)
            self.assertNotIn(wmic_dir + r"\wmic.exe", parts)
            self.assertNotEqual(env_out["PATH"], parent["PATH"])
            self.assertTrue(set(env_out) <= ALLOWED | AUTH_KEYS)
        self.assertEqual(env_build["GROK_HOME"], parent["GROK_HOME"])
        self.assertNotIn("XAI_API_KEY", env_build)

    def test_tool_dirs_come_after_system32(self):
        git_dir = r"X:\git-bin"
        parent = dict(PARENT)
        parent["PATH"] = git_dir
        home = Path("X") / "pane"
        with self._windows_nt({git_dir + r"\git.exe": True}):
            env = server.term_child_env(home, pane="1", environ=parent)
        parts = env["PATH"].split(";")
        idx_sys32 = parts.index("X:\\Windows\\System32")
        idx_root = parts.index("X:\\Windows")
        idx_wbem = parts.index("X:\\Windows\\System32\\Wbem")
        idx_ps = parts.index("X:\\Windows\\System32\\WindowsPowerShell\\v1.0")
        idx_git = parts.index(git_dir)
        self.assertLess(idx_sys32, idx_wbem)
        self.assertLess(idx_wbem, idx_ps)
        self.assertLess(idx_ps, idx_root)
        self.assertLess(idx_root, idx_git)
        self.assertLess(idx_sys32, idx_git)

    def test_path_tool_names_required_and_optional(self):
        self.assertEqual(server.PATH_REQUIRED_TOOLS, ("git.exe", "grok.exe"))
        self.assertEqual(server.PATH_OPTIONAL_TOOLS, ("wmic.exe",))
        self.assertEqual(server.PATH_TOOL_NAMES, server.PATH_REQUIRED_TOOLS + server.PATH_OPTIONAL_TOOLS)
        self.assertEqual(server.PATH_TOOL_NAMES, ("git.exe", "grok.exe", "wmic.exe"))
        self.assertNotIn("python.exe", server.PATH_TOOL_NAMES)
        src = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn("required/optional is documentation only", src.lower())
        self.assertNotIn("shutil.which", src)
        self.assertNotIn("import shutil", src)
        extra_dir = r"X:\cmd-bin"
        parent = dict(PARENT)
        parent["PATH"] = extra_dir
        home = Path("X") / "pane"
        with self._windows_nt({extra_dir + r"\cmd.exe": True}):
            env = server.term_child_env(home, pane="1", environ=parent)
        parts = env["PATH"].split(";")
        self.assertNotIn(extra_dir, parts)
        self.assertNotIn(extra_dir + r"\cmd.exe", parts)
        self.assertNotIn("cmd.exe", env["PATH"])

    def test_wmic_is_optional(self):
        git_dir, grok_dir = r"X:\git-bin", r"X:\grok-bin"
        parent = dict(PARENT)
        parent["PATH"] = ";".join((git_dir, grok_dir))
        home = Path("X") / "pane"
        with self._windows_nt({
            git_dir + r"\git.exe": True,
            grok_dir + r"\grok.exe": True,
        }):
            found = server.parent_path_tool_dirs(parent)
            env = server.term_child_env(home, pane="1", environ=parent)
        self.assertEqual(found, [git_dir, grok_dir])
        parts = env["PATH"].split(";")
        self.assertIn(git_dir, parts)
        self.assertIn(grok_dir, parts)
        self.assertNotIn(r"X:\wmic-bin", parts)

    def test_py_exe_dir_is_not_added(self):
        py_dir, python_dir = r"X:\py-bin", r"X:\python-bin"
        parent = dict(PARENT)
        parent["PATH"] = ";".join((py_dir, python_dir))
        home = Path("X") / "pane"
        with self._windows_nt({
            py_dir + r"\py.exe": True,
            python_dir + r"\python.exe": True,
        }):
            found = server.parent_path_tool_dirs(parent)
            env = server.term_child_env(home, pane="1", environ=parent)
        self.assertEqual(found, [])
        parts = env["PATH"].split(";")
        self.assertNotIn(py_dir, parts)
        self.assertNotIn(python_dir, parts)

    def test_skips_windowsapps_python_and_winget_stubs(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            apps = Path(tmp.name) / "Microsoft" / "WindowsApps"
            apps.mkdir(parents=True)
            (apps / "python.exe").write_bytes(b"")
            (apps / "winget.exe").write_bytes(b"")
            self.assertTrue((apps / "python.exe").is_file())
            self.assertTrue((apps / "winget.exe").is_file())
            win_apps = r"X:\Users\x\AppData\Local\Microsoft\WindowsApps"
            nested = win_apps + r"\PythonSoftwareFoundation"
            git_dir = r"X:\git-bin"
            parent = dict(PARENT)
            parent["PATH"] = ";".join((win_apps, nested, git_dir))
            home = Path("X") / "pane"
            with self._windows_nt({
                win_apps + r"\python.exe": True,
                win_apps + r"\winget.exe": True,
                win_apps + r"\git.exe": True,
                nested + r"\git.exe": True,
                git_dir + r"\git.exe": True,
            }):
                self.assertTrue(server._is_windows_apps_dir(win_apps))
                self.assertTrue(server._is_windows_apps_dir(nested))
                self.assertFalse(server._allowed_parent_path_dir(win_apps))
                self.assertFalse(server._allowed_parent_path_dir(nested))
                found = server.parent_path_tool_dirs(parent)
                env = server.term_child_env(home, pane="1", environ=parent)
            self.assertEqual(found, [git_dir])
            parts = env["PATH"].split(";")
            self.assertNotIn(win_apps, parts)
            self.assertNotIn(nested, parts)
            self.assertIn(git_dir, parts)
        finally:
            tmp.cleanup()

    def test_appends_folder_not_file_path(self):
        git_dir = r"X:\git-bin"
        git_file = git_dir + r"\git.exe"
        parent = dict(PARENT)
        parent["PATH"] = git_dir
        home = Path("X") / "pane"
        with self._windows_nt({git_file: True}):
            env = server.term_child_env(home, pane="1", environ=parent)
        parts = env["PATH"].split(";")
        self.assertIn(git_dir, parts)
        self.assertNotIn(git_file, parts)
        for part in parts:
            self.assertFalse(part.lower().endswith("git.exe"), part)
            self.assertFalse(part.lower().endswith("grok.exe"), part)
            self.assertFalse(part.lower().endswith("wmic.exe"), part)
            self.assertFalse(part.lower().endswith("py.exe"), part)
            self.assertFalse(part.lower().endswith("python.exe"), part)

    def test_rejects_relative_dot_empty_and_unc(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            root = Path(tmp.name)
            cwd = root / "cwd"
            cwd.mkdir()
            (cwd / "git.exe").write_text("", encoding="utf-8")
            (cwd / "git.exe").chmod(0o755)
            rel = cwd / "rel-git"
            rel.mkdir()
            (rel / "git.exe").write_text("", encoding="utf-8")
            (rel / "git.exe").chmod(0o755)
            abs_git, abs_file = r"X:\abs-git", r"X:\abs-git\git.exe"
            parent = dict(PARENT)
            parent["PATH"] = ";".join((
                "",
                ".",
                "..",
                "rel-git",
                str(rel.relative_to(cwd)),
                r"\\server\share\git",
                r"\\?\C:\Git\cmd",
                r"//server/share/git",
                abs_git,
            ))
            previous = os.getcwd()
            home = Path("X") / "pane"
            try:
                os.chdir(cwd)
                with self._windows_nt({abs_file: True, os.path.join(str(rel), "git.exe"): True}):
                    env = server.term_child_env(home, pane="1", environ=parent)
                    found = server.parent_path_tool_dirs(parent)
            finally:
                os.chdir(previous)
            parts = env["PATH"].split(";")
            self.assertEqual(found, [abs_git])
            self.assertIn(abs_git, parts)
            self.assertNotIn(abs_file, parts)
            self.assertNotIn(".", parts)
            self.assertNotIn("..", parts)
            self.assertNotIn("", parts)
            self.assertNotIn("rel-git", parts)
            self.assertNotIn(os.path.normpath(str(rel)), parts)
            self.assertFalse(any(part.startswith("\\\\") for part in parts))
            self.assertFalse(any(part.startswith("//") for part in parts))
            self.assertNotIn(os.path.normpath(str(cwd)), parts)
        finally:
            tmp.cleanup()

    def test_duplicate_and_trailing_slash_entries_dedupe(self):
        git_dir = r"X:\git-bin"
        parent = dict(PARENT)
        parent["PATH"] = ";".join((
            git_dir + "/",
            git_dir,
            git_dir + os.sep,
        ))
        home = Path("X") / "pane"
        with self._windows_nt({git_dir + r"\git.exe": True}):
            found = server.parent_path_tool_dirs(parent)
            env = server.term_child_env(home, pane="1", environ=parent)
        self.assertEqual(found, [os.path.normpath(git_dir)])
        self.assertEqual(env["PATH"].split(";").count(os.path.normpath(git_dir)), 1)

    def test_system32_in_parent_path_is_not_duplicated(self):
        git_dir = r"X:\git-bin"
        parent = dict(PARENT)
        parent["PATH"] = ";".join((
            "X:\\Windows\\System32",
            "X:\\Windows\\System32\\",
            git_dir,
        ))
        home = Path("X") / "pane"
        with self._windows_nt({git_dir + r"\git.exe": True}):
            env = server.term_child_env(home, pane="1", environ=parent)
        parts = env["PATH"].split(";")
        self.assertEqual(parts.count("X:\\Windows\\System32"), 1)
        self.assertLess(parts.index("X:\\Windows\\System32"), parts.index(git_dir))
        self.assertLess(parts.index("X:\\Windows\\System32\\Wbem"), parts.index(git_dir))

    def test_systemroot_in_parent_path_is_not_duplicated(self):
        parent = dict(PARENT)
        parent["PATH"] = "X:\\Windows"
        home = Path("X") / "pane"
        with self._windows_nt({r"X:\Windows\py.exe": True}):
            env = server.term_child_env(home, pane="1", environ=parent)
        parts = env["PATH"].split(";")
        self.assertEqual(parts.count("X:\\Windows"), 1)
        self.assertLess(parts.index("X:\\Windows\\System32"), parts.index("X:\\Windows"))
        self.assertLess(parts.index("X:\\Windows\\System32\\Wbem"), parts.index("X:\\Windows"))
        self.assertLess(
            parts.index("X:\\Windows\\System32\\WindowsPowerShell\\v1.0"),
            parts.index("X:\\Windows"),
        )

    def test_two_found_folders_keep_parent_path_order(self):
        git_dir, wmic_dir = r"X:\git-bin", r"X:\wmic-bin"
        home = Path("X") / "pane"
        parent = dict(PARENT)
        files = {git_dir + r"\git.exe": True, wmic_dir + r"\wmic.exe": True}
        parent["PATH"] = ";".join((git_dir, wmic_dir))
        with self._windows_nt(files):
            found = server.parent_path_tool_dirs(parent)
            env = server.term_child_env(home, pane="1", environ=parent)
        self.assertEqual(found, [git_dir, wmic_dir])
        parts = env["PATH"].split(";")
        self.assertLess(parts.index(git_dir), parts.index(wmic_dir))
        self.assertLess(parts.index("X:\\Windows\\System32"), parts.index(git_dir))
        parent["PATH"] = ";".join((wmic_dir, git_dir))
        with self._windows_nt(files):
            found_rev = server.parent_path_tool_dirs(parent)
            env_rev = server.term_child_env(home, pane="1", environ=parent)
        self.assertEqual(found_rev, [wmic_dir, git_dir])
        parts_rev = env_rev["PATH"].split(";")
        self.assertLess(parts_rev.index(wmic_dir), parts_rev.index(git_dir))

    def test_directory_named_git_exe_is_rejected(self):
        folder = r"X:\looks-like-git"
        parent = dict(PARENT)
        parent["PATH"] = folder
        home = Path("X") / "pane"
        with self._windows_nt({folder + r"\git.exe": False}):
            found = server.parent_path_tool_dirs(parent)
            env = server.term_child_env(home, pane="1", environ=parent)
        self.assertEqual(found, [])
        self.assertNotIn(folder, env["PATH"].split(";"))

    def test_rejects_rooted_no_drive_paths(self):
        self.assertFalse(server._allowed_parent_path_dir(r"\Tools"))
        self.assertFalse(server._allowed_parent_path_dir(r"\Tools\git"))
        self.assertFalse(server._allowed_parent_path_dir("/Tools"))
        self.assertFalse(server._allowed_parent_path_dir("/Tools/"))
        self.assertTrue(server._is_rooted_no_drive("/Tools"))
        real_isabs = os.path.isabs
        real_isfile = os.path.isfile

        def windows_isabs(path):
            text = str(path)
            slashes = text.replace("/", "\\")
            if slashes.startswith("\\") and not slashes.startswith("\\\\"):
                return True
            return real_isabs(path)

        def fake_isfile(path):
            text = str(path).replace("\\", "/")
            if text.endswith("/git.exe") and "Tools" in text:
                return True
            return real_isfile(path)

        parent = dict(PARENT)
        parent["PATH"] = ";".join((r"\Tools", r"\Tools\git", "/Tools", "/Tools/"))
        with mock.patch.object(os.path, "isabs", side_effect=windows_isabs), mock.patch.object(
            os.path, "isfile", side_effect=fake_isfile
        ):
            self.assertFalse(server._allowed_parent_path_dir(r"\Tools"))
            self.assertFalse(server._allowed_parent_path_dir("/Tools"))
            self.assertEqual(server.parent_path_tool_dirs(parent), [])

    def test_rejects_multipart_rooted_paths_on_nt(self):
        home = Path("X") / "pane"
        parent = dict(PARENT)
        parent["PATH"] = ";".join((
            "/Tools/bin",
            r"\Tools\bin",
            "/Tools/bin/",
            r"C:\Tools\bin",
        ))
        with self._windows_nt({
            r"/Tools/bin/git.exe": True,
            r"\Tools\bin\git.exe": True,
            r"C:\Tools\bin\git.exe": True,
        }):
            self.assertTrue(server._is_rooted_no_drive("/Tools/bin"))
            self.assertTrue(server._is_rooted_no_drive(r"\Tools\bin"))
            self.assertFalse(server._allowed_parent_path_dir("/Tools/bin"))
            self.assertFalse(server._allowed_parent_path_dir(r"\Tools\bin"))
            self.assertFalse(server._is_rooted_no_drive(r"C:\Tools\bin"))
            found = server.parent_path_tool_dirs(parent)
            env = server.term_child_env(home, pane="1", environ=parent)
        self.assertEqual(found, [os.path.normpath(r"C:\Tools\bin")])
        parts = env["PATH"].split(";")
        self.assertNotIn("/Tools/bin", parts)
        self.assertNotIn(r"\Tools\bin", parts)
        self.assertIn(os.path.normpath(r"C:\Tools\bin"), parts)

    def test_term_dedup_kills_m3_duplicate_system32(self):
        """M3: drop the seen-key check in _windows_term_path and System32 is appended twice."""
        home = Path("X") / "pane"
        parent = dict(PARENT)
        found = [
            "X:\\Windows\\System32",
            "X:\\Windows\\System32\\Wbem",
        ]
        with mock.patch.object(os, "name", "nt"), mock.patch.object(
            server, "parent_path_tool_dirs", return_value=found
        ):
            env = server.term_child_env(home, pane="1", environ=parent)
        parts = env["PATH"].split(";")
        self.assertEqual(parts.count("X:\\Windows\\System32"), 1)
        self.assertEqual(parts.count("X:\\Windows\\System32\\Wbem"), 1)
        self.assertEqual(
            parts,
            [
                str(server.GROK.parent),
                "X:\\Windows\\System32",
                "X:\\Windows\\System32\\Wbem",
                "X:\\Windows\\System32\\WindowsPowerShell\\v1.0",
                "X:\\Windows",
            ],
        )

    def test_bad_systemroot_falls_back_to_windir(self):
        parent = dict(PARENT)
        parent["SYSTEMROOT"] = r"\\evil\Windows"
        parent["WINDIR"] = r"X:\Windows"
        home = Path("X") / "pane"
        with mock.patch.object(os, "name", "nt"):
            env = server.term_child_env(home, pane="1", environ=parent)
        parts = env["PATH"].split(";")
        self.assertEqual(parts, _win_path().split(";"))
        self.assertNotIn(r"\\evil\Windows", env["PATH"])

    def test_relative_systemroot_and_windir_use_empty_default(self):
        parent = dict(PARENT)
        parent["SYSTEMROOT"] = "Windows"
        parent["WINDIR"] = r"\Windows"
        home = Path("X") / "pane"
        with mock.patch.object(os, "name", "nt"):
            env = server.term_child_env(home, pane="1", environ=parent)
        self.assertEqual(env["PATH"], str(server.GROK.parent))

    def test_rejects_c_windows_without_separator(self):
        self.assertFalse(server._safe_windows_root("C:Windows"))
        self.assertTrue(server._safe_windows_root(r"C:\Windows"))
        parent = dict(PARENT)
        parent["SYSTEMROOT"] = "C:Windows"
        parent["WINDIR"] = r"X:\Windows"
        home = Path("X") / "pane"
        with mock.patch.object(os, "name", "nt"):
            env = server.term_child_env(home, pane="1", environ=parent)
        self.assertEqual(env["PATH"].split(";"), _win_path().split(";"))
        self.assertNotIn("C:Windows", env["PATH"])

    def test_rejects_c_slash_drive_root(self):
        self.assertFalse(server._safe_windows_root("C:\\"))
        self.assertFalse(server._safe_windows_root("C:/"))
        self.assertTrue(server._safe_windows_root(r"X:\Windows"))
        parent = dict(PARENT)
        parent["SYSTEMROOT"] = "C:\\"
        parent["WINDIR"] = r"X:\Windows"
        home = Path("X") / "pane"
        with mock.patch.object(os, "name", "nt"):
            env = server.term_child_env(home, pane="1", environ=parent)
        self.assertEqual(env["PATH"].split(";"), _win_path().split(";"))
        self.assertNotIn("C:\\System32", env["PATH"])

    def test_build_default_grok_home_under_profile(self):
        extra = dict(PARENT)
        extra.pop("GROK_HOME")
        extra["XAI_API_KEY"] = "xai-test-not-real"
        home = Path("X") / "panes" / "pane-build"
        with mock.patch.object(os, "name", "nt"):
            env = server.term_child_env(home, pane="build", environ=extra)
        self.assertEqual(env["GROK_HOME"], "X:\\profile\\.grok")
        self.assertEqual(env["USERPROFILE"], "X:\\profile")
        self.assertEqual(env["APPDATA"], "X:\\roaming")
        self.assertEqual(env["LOCALAPPDATA"], "X:\\local")
        self.assertEqual(env["HOMEDRIVE"], "X:")
        self.assertEqual(env["HOMEPATH"], "\\profile")
        self.assertNotIn("XAI_API_KEY", env)
        self.assertNotIn("GROK_API_KEY", env)
        self.assertNotIn("SECRET", env)


if __name__ == "__main__":
    unittest.main(verbosity=2)
