"""Default build folder is local-app or the app folder, never drive-root AOS."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server


class TestBuildCwdDefault(unittest.TestCase):
    def test_source_has_no_drive_root_aos(self):
        text = (ROOT / "server.py").read_text(encoding="utf-8")
        self.assertNotIn('Path(ROOT.anchor) / "AOS"', text)
        self.assertNotIn('ROOT.anchor) / "AOS"', text)
        self.assertIn("LOCALAPPDATA", text)
        self.assertIn("LPIN", text)
        self.assertIn("build-work", text)
        self.assertIn("HBC_BUILD_CWD", text)

    def test_localappdata_lpin_when_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "Local"
            got = server.default_build_cwd(
                root=ROOT,
                environ={"LOCALAPPDATA": str(local)},
            )
        self.assertEqual(got, str(local / "LPIN" / "build"))
        self.assertNotIn("AOS", Path(got).parts)

    def test_app_folder_when_localappdata_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "app"
            got = server.default_build_cwd(root=app, environ={})
        self.assertEqual(got, str(app / "build-work"))
        self.assertNotIn("AOS", Path(got).parts)

    def test_env_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "custom-build"
            got = server.default_build_cwd(
                root=ROOT,
                environ={
                    "HBC_BUILD_CWD": str(custom),
                    "LOCALAPPDATA": str(Path(tmp) / "Local"),
                },
            )
        self.assertEqual(got, str(custom))

    def test_module_default_is_not_drive_root_aos(self):
        self.assertNotEqual(server.BUILD_CWD, str(Path(server.ROOT.anchor) / "AOS"))
        self.assertFalse(str(server.BUILD_CWD).rstrip("\\/").endswith(os.sep + "AOS"))
        self.assertFalse(str(server.BUILD_CWD).replace("\\", "/").endswith("/AOS"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
