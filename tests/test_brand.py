"""LPIN brand: public compass-rose fallback, gitignored private files, no hexagon."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from tests.runtime_skip import skip_symlink_denied

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from hub import brand

GITIGNORE = (ROOT / ".gitignore").read_text(encoding="utf-8")
FORM = (ROOT / "shell" / "Form1.cs").read_text(encoding="utf-8")
CONTROL = (ROOT / "shell" / "ControlTab.cs").read_text(encoding="utf-8")
ASSETS = (ROOT / "shell" / "BrandAssets.cs").read_text(encoding="utf-8")
SVG = ROOT / "brand" / "lpin-compass-outline.svg"

LOCAL_BRAND_GITIGNORE = ROOT / "tests" / "fixtures" / "brand-dir.ignore"

PRIVATE_NAMES = (
    "compass-mark-private-16.png",
    "compass-mark-private-32.png",
    "compass-mark-private-64.png",
    "compass-mark-private-256.png",
    "compass-mark-private-512.png",
    "compass-mark-private.ico",
    "control-header-texture-private.jpg",
)

SKIP_DIRS = {".git", "bin", "obj", "profiles", "local-brand"}


def _committed_ui_files():
    kept = []
    for path in list(ROOT.rglob("*.svg")) + list(ROOT.rglob("*.css")) + list(ROOT.rglob("*.cs")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        kept.append(path)
    return kept


class TestBrand(unittest.TestCase):
    def test_product_name_is_lpin(self):
        self.assertEqual(brand.PRODUCT_NAME, "LPIN")
        self.assertIn('ProductName = "LPIN"', ASSETS)
        self.assertIn("Text = BrandAssets.ProductName", FORM)
        self.assertIn("BrandAssets.ProductName", CONTROL)
        self.assertNotIn("hbc-mark.svg", FORM + CONTROL + ASSETS)
        self.assertNotIn("hbc-mark", FORM + CONTROL + ASSETS + (ROOT / "hub" / "brand.py").read_text(encoding="utf-8"))

    def test_private_names_are_exact(self):
        self.assertEqual(brand.PRIVATE_ICO, "compass-mark-private.ico")
        self.assertEqual(brand.PRIVATE_HEADER, "control-header-texture-private.jpg")
        self.assertEqual(
            [brand.MARK_PNG[size] for size in (16, 32, 64, 256, 512)],
            list(PRIVATE_NAMES[:5]),
        )
        for name in PRIVATE_NAMES:
            self.assertIn('"' + name + '"', ASSETS + "\n" + (ROOT / "hub" / "brand.py").read_text(encoding="utf-8"))

    def test_fallback_when_private_files_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "brand").mkdir()
            svg = root / brand.FALLBACK_SVG
            svg.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>\n", encoding="utf-8")
            for name in PRIVATE_NAMES:
                path = brand.resolve(root, name)
                self.assertEqual(path.resolve(), svg.resolve(), name)
                self.assertTrue(brand.is_fallback(path, root), name)
            self.assertTrue(brand.is_fallback(brand.window_icon(root), root))
            self.assertTrue(brand.is_fallback(brand.mark_png(root, 32), root))
            self.assertTrue(brand.is_fallback(brand.header_texture(root), root))
            self.assertTrue(brand.is_fallback(brand.mark_png(root, 7), root))

    def test_loader_uses_private_file_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "brand").mkdir()
            svg = root / brand.FALLBACK_SVG
            svg.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>\n", encoding="utf-8")
            private = root / brand.PRIVATE_DIR
            private.mkdir()
            ico = private / brand.PRIVATE_ICO
            ico.write_bytes(b"ico")
            png = private / brand.MARK_PNG[32]
            png.write_bytes(b"png")
            jpg = private / brand.PRIVATE_HEADER
            jpg.write_bytes(b"jpg")
            self.assertEqual(brand.window_icon(root), ico)
            self.assertEqual(brand.mark_png(root, 32), png)
            self.assertEqual(brand.header_texture(root), jpg)
            self.assertFalse(brand.is_fallback(ico, root))
            self.assertTrue(brand.is_fallback(brand.mark_png(root, 16), root))

    def test_loader_skips_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "brand").mkdir()
            svg = root / brand.FALLBACK_SVG
            svg.write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>\n", encoding="utf-8")
            private = root / brand.PRIVATE_DIR
            private.mkdir()
            target = root / "outside.ico"
            target.write_bytes(b"x")
            link = private / brand.PRIVATE_ICO
            try:
                if os.name == "nt":
                    target_dir = root / "outside-dir"
                    target_dir.mkdir()
                    try:
                        import _winapi
                        _winapi.CreateJunction(str(target_dir), str(link))
                    except (AttributeError, OSError) as exc:
                        skip_symlink_denied(self, exc)
                else:
                    link.symlink_to(target)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    skip_symlink_denied(self, exc)
                self.skipTest("symlink not available")
            self.assertTrue(brand.is_fallback(brand.window_icon(root), root))

    def test_local_brand_gitignore_fixture(self):
        fixture = LOCAL_BRAND_GITIGNORE.read_text(encoding="utf-8")
        self.assertIn("local-brand/", fixture)
        self.assertIn("local-brand/", GITIGNORE)
        self.assertEqual(fixture.strip(), "local-brand/")

    def _require_git(self):
        git_dir = ROOT / ".git"
        if not git_dir.exists():
            self.skipTest(".git is missing")
        listed = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        if listed.returncode != 0:
            self.skipTest("not a git work tree")

    def test_local_brand_is_gitignored_and_untracked(self):
        self.assertIn("local-brand/", GITIGNORE)
        self._require_git()
        listed = subprocess.run(
            ["git", "ls-files", "--", "local-brand", "local-brand/**"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(listed.stdout.strip(), "")
        ignored = subprocess.run(
            ["git", "check-ignore", "-v", "local-brand/compass-mark-private.ico"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(ignored.returncode, 0, ignored.stdout + ignored.stderr)
        self.assertIn("local-brand", ignored.stdout)

    def test_no_private_or_local_brand_file_is_tracked(self):
        self._require_git()
        listed = subprocess.run(
            ["git", "ls-files"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        tracked = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        for path in tracked:
            self.assertNotIn("local-brand", path)
            name = Path(path).name.lower()
            self.assertNotIn("private", name, path)

    def test_no_hbc_mark_and_no_hexagon_in_committed_svg_css_cs(self):
        self.assertFalse((ROOT / "hbc-mark.svg").exists())
        self.assertFalse((ROOT / "brand" / "hbc-mark.svg").exists())
        for path in _committed_ui_files():
            text = path.read_text(encoding="utf-8")
            lower = text.lower()
            self.assertNotIn("hbc-mark", lower, path)
            self.assertNotIn("hexagon", lower, path)
            self.assertNotIn("hex-motif", lower, path)
            self.assertNotIn("<polygon", lower, path)
            self.assertNotIn("clip-path: polygon", lower, path)

    def test_fallback_svg_is_a_plain_compass_outline(self):
        self.assertTrue(SVG.is_file())
        raw = SVG.read_text(encoding="utf-8")
        self.assertIn("LPIN", raw)
        self.assertNotIn("hexagon", raw.lower())
        self.assertNotIn("<polygon", raw.lower())
        tree = ET.fromstring(raw)
        tags = {el.tag.split("}")[-1] for el in tree.iter()}
        self.assertTrue({"svg", "circle", "path", "line"} <= tags or {"svg", "circle", "line"} <= tags)
        self.assertNotIn("polygon", tags)

    def test_shell_wires_icon_header_and_fallback(self):
        self.assertIn("BrandAssets.ApplyWindowIcon(this, root)", FORM)
        self.assertIn("BrandAssets.TitleBarMark(root)", FORM)
        self.assertIn("BrandAssets.TabIcon(root)", FORM)
        self.assertIn("BrandAssets.MakeSplash(root)", FORM)
        self.assertIn("BrandAssets.ControlHeaderTexture(root)", CONTROL)
        self.assertIn("ExistingFile(root, PrivateIco)", ASSETS)
        self.assertIn("FallbackSvgPath(root)", ASSETS)
        self.assertIn("OutlineBitmap", ASSETS)
        self.assertIn('FallbackSvg = "brand/lpin-compass-outline.svg"', ASSETS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
