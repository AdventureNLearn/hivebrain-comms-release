"""Configurable bus lanes: Hub/Builder defaults, local file, custom names."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hub import jobs, lanes, protocol, sigverify
import server
from tests.legacy_lanes import LEGACY, install


CUSTOM_DATA = {
    "status": "ALPHA-STATUS.md",
    "lanes": [
        {
            "id": "Alpha",
            "owner": "Alpha",
            "inbox": "ALPHA-TO-BUILD.md",
            "outbox": "BUILD-TO-ALPHA.md",
            "key": "bus-alpha.bin",
        },
        {
            "id": "Beta",
            "owner": "Beta",
            "inbox": "BETA-TO-BUILD.md",
            "outbox": "BUILD-TO-BETA.md",
            "key": "bus-beta.bin",
        },
    ],
}


class TestLaneDefaults(unittest.TestCase):
    def test_shipped_defaults_are_hub_builder(self):
        spec = lanes.default_spec()
        self.assertEqual(spec.owners["HUB-TO-BUILD.md"], "Hub")
        self.assertEqual(spec.owners["BUILDER-TO-BUILD.md"], "Builder")
        self.assertEqual(spec.returns["Hub"], "BUILD-TO-HUB.md")
        self.assertEqual(spec.returns["Builder"], "BUILD-TO-BUILDER.md")
        self.assertEqual(spec.key_files["Hub"], "bus-hub.bin")
        self.assertEqual(spec.key_files["Builder"], "bus-builder.bin")
        self.assertEqual(spec.status, "BUILDER-STATUS.md")
        self.assertEqual(spec.routers, ("Hub", "Builder"))
        self.assertNotIn("LEAD-TO-BUILD.md", spec.owners)
        self.assertNotIn("GROKBOT-TO-BUILD.md", spec.owners)
        self.assertNotIn("Lead", spec.aliases)
        self.assertNotIn("Grok Bot", spec.aliases)

    def test_production_sources_load_lanes_not_hardcoded_lead(self):
        server_src = (ROOT / "server.py").read_text(encoding="utf-8")
        jobs_src = (ROOT / "hub" / "jobs.py").read_text(encoding="utf-8")
        verify_src = (ROOT / "hub" / "sigverify.py").read_text(encoding="utf-8")
        logic = (ROOT / "shell" / "ControlLogic.cs").read_text(encoding="utf-8")
        self.assertIn("lanes.load()", server_src)
        self.assertNotIn('OWNERS = {"LEAD-TO-BUILD.md"', server_src)
        self.assertIn("lanes.load()", jobs_src)
        self.assertNotIn('"LEAD-TO-BUILD.md": "Lead"', jobs_src)
        self.assertIn("_lanes.load()", verify_src)
        self.assertNotIn('"Lead": "bus-lead.bin"', verify_src)
        self.assertIn('["HUB-TO-BUILD.md"] = "Hub"', logic)
        self.assertIn('["BUILDER-TO-BUILD.md"] = "Builder"', logic)
        self.assertNotIn('["LEAD-TO-BUILD.md"]', logic)
        self.assertIn("lanes.local.json", logic)
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("lanes.local.json", gitignore)

    def test_signer_loads_lanes_and_keeps_cli_owners(self):
        script = (ROOT / "tools" / "Sign-BusJob.ps1").read_text(encoding="utf-8")
        self.assertIn("Get-LaneTable", script)
        self.assertIn("lanes.local.json", script)
        self.assertIn("bus-hub.bin", script)
        self.assertIn("bus-builder.bin", script)
        self.assertIn("bus-lead.bin", script)
        self.assertIn("bus-grokbot.bin", script)
        self.assertIn("must be Grok Bot or Lead plus ISO-8601", script)
        self.assertIn("signing key is missing", script)


class TestCustomLanes(unittest.TestCase):
    def tearDown(self):
        install()

    def test_apply_custom_lanes_rebinds_hub_maps(self):
        spec = lanes.parse(CUSTOM_DATA)
        self.assertIsNotNone(spec)
        install(spec)
        self.assertEqual(server.OWNERS["ALPHA-TO-BUILD.md"], "Alpha")
        self.assertEqual(server.RETURNS["Beta"], "BUILD-TO-BETA.md")
        self.assertNotIn("LEAD-TO-BUILD.md", server.OWNERS)
        self.assertEqual(jobs.STATUS_FILE, "ALPHA-STATUS.md")
        self.assertEqual(jobs.OWNERS["BETA-TO-BUILD.md"], "Beta")
        self.assertEqual(sigverify.KEY_FILES["Alpha"], "bus-alpha.bin")
        self.assertEqual(sigverify.normalize_owner("Alpha"), "Alpha")
        self.assertIsNone(sigverify.normalize_owner("Lead"))
        self.assertEqual(protocol.ALLOWED_ROUTERS, ("Alpha", "Beta"))
        self.assertIn("ALPHA-TO-BUILD.md", server.WATCHER_PROMPT)
        self.assertIn("BETA-TO-BUILD.md", server.WATCHER_PROMPT)
        self.assertNotIn("LEAD-TO-BUILD.md", server.WATCHER_PROMPT)

    def test_custom_owner_signs_and_legacy_owner_is_refused(self):
        spec = lanes.parse(CUSTOM_DATA)
        install(spec)
        key = bytearray(os.urandom(32))
        sigverify.set_key_provider(lambda owner: bytearray(key) if owner == "Alpha" else None)
        try:
            fields = {
                "v": "2",
                "id": "lane-1",
                "from": "Alpha",
                "to": "Build",
                "lane": "lab",
                "priority": "normal",
                "time": "2026-10-10 12:00",
                "routed": "Alpha 2026-10-10T12:00:00-04:00",
                "pane": "1",
                "job": "list lab pane files",
                "done-when": "the pane lists its files",
            }
            fields["sig"] = sigverify.mac_hex(fields, key)
            self.assertEqual(sigverify.verify_job(fields, "Alpha", seen=set()), "")
            self.assertEqual(sigverify.verify_job(fields, "Lead", seen=set()), "OWNER_MISMATCH")
            missing = dict(fields)
            missing["from"] = "Lead"
            missing["sig"] = sigverify.mac_hex(missing, key)
            self.assertEqual(sigverify.verify_job(missing, "Alpha", seen=set()), "OWNER_MISMATCH")
        finally:
            sigverify.clear_key_provider()

    def test_missing_custom_key_fails_closed(self):
        spec = lanes.parse(CUSTOM_DATA)
        install(spec)
        sigverify.clear_key_provider()
        sigverify.set_keys_dir(tempfile.mkdtemp())
        try:
            fields = {
                "v": "2",
                "id": "lane-nokey",
                "from": "Alpha",
                "to": "Build",
                "lane": "lab",
                "priority": "normal",
                "time": "2026-10-10 12:00",
                "routed": "Alpha 2026-10-10T12:00:00-04:00",
                "pane": "1",
                "job": "list lab pane files",
                "done-when": "the pane lists its files",
                "sig": "0" * 64,
            }
            self.assertEqual(sigverify.verify_job(fields, "Alpha", seen=set()), "NO_KEY")
        finally:
            sigverify.set_keys_dir(None)

    def test_load_local_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lanes.local.json"
            path.write_text(json.dumps(CUSTOM_DATA), encoding="utf-8")
            spec = lanes.load(path)
            self.assertEqual(spec.owners["ALPHA-TO-BUILD.md"], "Alpha")
            self.assertEqual(spec.key_files["Beta"], "bus-beta.bin")
            missing = lanes.load(Path(tmp) / "absent.json")
            self.assertEqual(missing.routers, ("Hub", "Builder"))

    def test_invalid_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lanes.local.json"
            path.write_text("{not-json", encoding="utf-8")
            spec = lanes.load(path)
            self.assertEqual(spec.routers, ("Hub", "Builder"))
            path.write_text(json.dumps({"lanes": [{"owner": "../x", "inbox": "a.md"}]}), encoding="utf-8")
            spec = lanes.load(path)
            self.assertEqual(spec.routers, ("Hub", "Builder"))

    def test_legacy_helper_restores_existing_tests(self):
        install()
        self.assertEqual(server.OWNERS["LEAD-TO-BUILD.md"], "Lead")
        self.assertEqual(server.OWNERS["GROKBOT-TO-BUILD.md"], "Grok Bot")
        self.assertEqual(protocol.ALLOWED_ROUTERS, ("Grok Bot", "Lead"))
        self.assertEqual(sigverify.KEY_FILES["Lead"], "bus-lead.bin")
        self.assertEqual(jobs.STATUS_FILE, "GROKBOT-STATUS.md")
        self.assertEqual(LEGACY.routers, ("Grok Bot", "Lead"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
