"""The hub ignores HBC_TEST_SIGN_KEY_PATH. No raw-key staging mode in this tree."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server
from hub import sigverify
from tests.live_guard import install_live_state_guard, point_hub_at, restore_hub

install_live_state_guard()


class TestTestSignKeyIgnored(unittest.TestCase):
    def test_hub_source_has_no_test_sign_key_mode(self):
        src = Path(server.__file__).read_text(encoding="utf-8")
        start = src.split("async def start_hub", 1)[1].split("\ndef ", 1)[0]
        for needle in (
            "HBC_TEST_SIGN_KEY_PATH",
            "apply_test_sign_key",
            "TEST_SIGN_KEY_ENV",
            "STAGING_MARKER_NAME",
            "is_live_hub_root",
            "set_key_provider",
            "HBC test sign key ON",
        ):
            self.assertNotIn(needle, src, needle)
            self.assertNotIn(needle, start, needle)
        self.assertFalse(hasattr(server, "apply_test_sign_key"))
        self.assertFalse(hasattr(server, "is_live_hub_root"))
        self.assertFalse(hasattr(server, "TEST_SIGN_KEY_ENV"))
        self.assertFalse(hasattr(server, "STAGING_MARKER_NAME"))
        self.assertNotIn("set_key_provider", src)

    def test_hub_ignores_hbc_test_sign_key_path_env(self):
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        key = home / "raw.key"
        key.write_bytes(os.urandom(32))
        prev = point_hub_at(home)
        prev_provider = sigverify._key_provider
        sigverify.clear_key_provider()
        try:
            with patch.dict(os.environ, {"HBC_TEST_SIGN_KEY_PATH": str(key)}, clear=False):
                self.assertEqual(os.environ.get("HBC_TEST_SIGN_KEY_PATH"), str(key))
                self.assertFalse(hasattr(server, "apply_test_sign_key"))
                self.assertIsNone(sigverify._key_provider)
                src = Path(server.__file__).read_text(encoding="utf-8")
                self.assertNotIn("HBC_TEST_SIGN_KEY_PATH", src)
                self.assertNotIn("set_key_provider", src)
        finally:
            sigverify.set_key_provider(prev_provider)
            restore_hub(prev)
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
