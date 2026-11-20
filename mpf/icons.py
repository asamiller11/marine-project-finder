"""Builds the PNG and ICO copies of the logo from the SVG sources in mpf/assets.

mpf/assets/logo.svg is the one drawing of the mark (a map pin with a sea fan coral). mpf/assets/favicon.svg is its
cut-down version for browser tabs (pin and three thick branches), because the full coral blurs at 16 px.
Run `python -m mpf.icons` after changing either file. It needs the Chromium that Playwright installs
(~/Library/Caches/ms-playwright); nothing is added to requirements.txt. The weekly update only copies the files.
"""
from __future__ import annotations

import glob
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ASSETS = Path(__file__).parent / "assets"
# (output, source, size in px, background): apple-touch-icon needs an opaque square, iOS draws transparency black.
PNGS = [("favicon-16.png", "favicon.svg", 16, "transparent"),
        ("favicon-32.png", "favicon.svg", 32, "transparent"),
        ("apple-touch-icon.png", "logo.svg", 180, "#ffffff"),
        ("logo-144.png", "logo.svg", 144, "transparent")]


def _chromium() -> str:
    pats = ["~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
            "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell"]
    found = sorted(p for pat in pats for p in glob.glob(os.path.expanduser(pat)))
    if not found:
        sys.exit("Playwright's Chromium was not found; install it with `npx playwright install chromium-headless-shell`.")
    return found[-1]


def render_png(svg: Path, out: Path, size: int, background: str) -> None:
    pad = round(size * 0.12) if background != "transparent" else 0
    html = (f'<html><body style="margin:0;background:{background};width:{size}px;height:{size}px;display:flex;'
            f'align-items:center;justify-content:center"><img src="{svg.resolve().as_uri()}" '
            f'style="height:{size - 2 * pad}px"></body></html>')
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "icon.html"
        page.write_text(html, encoding="utf-8")
        args = [_chromium(), "--headless", "--disable-gpu", "--hide-scrollbars", "--allow-file-access-from-files",
                "--force-device-scale-factor=1", f"--window-size={size},{size}", "--virtual-time-budget=1000",
                f"--screenshot={out.resolve()}", page.as_uri()]
        if background == "transparent":
            args.insert(2, "--default-background-color=00000000")
        subprocess.run(args, check=True, capture_output=True)


def ico_from_pngs(pngs: list[Path], out: Path) -> None:
    """Windows icon file that holds the PNGs as they are (allowed since Windows Vista)."""
    blobs = [p.read_bytes() for p in pngs]
    head = struct.pack("<HHH", 0, 1, len(blobs))
    offset, entries = 6 + 16 * len(blobs), b""
    for b in blobs:
        w, h = struct.unpack(">II", b[16:24])
        entries += struct.pack("<BBBBHHII", w % 256, h % 256, 0, 0, 1, 32, len(b), offset)
        offset += len(b)
    out.write_bytes(head + entries + b"".join(blobs))


def main() -> None:
    for name, src, size, bg in PNGS:
        render_png(ASSETS / src, ASSETS / name, size, bg)
        print("wrote", ASSETS / name)
    ico_from_pngs([ASSETS / "favicon-16.png", ASSETS / "favicon-32.png"], ASSETS / "favicon.ico")
    print("wrote", ASSETS / "favicon.ico")


if __name__ == "__main__":
    main()
