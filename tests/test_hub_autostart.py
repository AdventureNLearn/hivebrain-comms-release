"""Hub auto-start is off unless the user opts in."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORM = (ROOT / "shell" / "Form1.cs").read_text(encoding="utf-8")


def _method(source, signature):
    start = source.find(signature)
    if start < 0:
        return ""
    rest = source[start:]
    nxt = rest.find("\n    private ", len(signature))
    if nxt < 0:
        nxt = rest.find("\n    public ", len(signature))
    return rest if nxt < 0 else rest[:nxt]


class TestHubAutoStartOff(unittest.TestCase):
    def test_field_defaults_false(self):
        self.assertIn("private bool autoStartHub = false;", FORM)
        self.assertNotIn("private bool autoStartHub = true;", FORM)

    def test_load_settings_defaults_off(self):
        load = _method(FORM, "private void LoadSettings()")
        self.assertIn("autoStartHub = false;", load)
        self.assertNotIn("autoStartHub = true;", load)
        self.assertIn("auto-start-hub=", load)
        self.assertIn('line.EndsWith("on", StringComparison.Ordinal)', load)

    def test_save_settings_writes_opt_in_flag(self):
        save = _method(FORM, "private void SaveSettings()")
        self.assertIn("auto-start-hub=", save)
        self.assertIn('autoStartHub ? "on" : "off"', save)

    def test_checkbox_is_opt_in(self):
        ctor = _method(FORM, "public Form1()")
        self.assertIn('Text = "Auto-start hub"', ctor)
        self.assertIn("autoStart.Checked = autoStartHub;", ctor)
        self.assertIn("autoStartHub = autoStart.Checked", ctor)


if __name__ == "__main__":
    unittest.main(verbosity=2)
