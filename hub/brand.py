"""LPIN brand paths. Private files stay in gitignored local-brand/."""

from pathlib import Path

PRODUCT_NAME = "LPIN"
PRIVATE_DIR = "local-brand"
FALLBACK_SVG = "brand/lpin-compass-outline.svg"
PRIVATE_ICO = "compass-mark-private.ico"
PRIVATE_HEADER = "control-header-texture-private.jpg"
MARK_PNG = {
    16: "compass-mark-private-16.png",
    32: "compass-mark-private-32.png",
    64: "compass-mark-private-64.png",
    256: "compass-mark-private-256.png",
    512: "compass-mark-private-512.png",
}


def private_dir(root):
    return Path(root) / PRIVATE_DIR


def fallback_svg(root):
    return Path(root) / FALLBACK_SVG


def resolve(root, name):
    """Private file if present; otherwise the public compass-rose outline."""
    private = private_dir(root) / name
    if private.is_file() and not private.is_symlink():
        return private
    return fallback_svg(root)


def window_icon(root):
    return resolve(root, PRIVATE_ICO)


def mark_png(root, size):
    name = MARK_PNG.get(int(size))
    if not name:
        return fallback_svg(root)
    return resolve(root, name)


def header_texture(root):
    return resolve(root, PRIVATE_HEADER)


def is_fallback(path, root):
    try:
        return Path(path).resolve() == fallback_svg(root).resolve()
    except OSError:
        return False
