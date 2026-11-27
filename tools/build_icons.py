#!/usr/bin/env python3
"""Draw calsync's mark and write every size the app, the website and the docs use.

The mark follows LocalHub's: a rounded square with a dark slate gradient and one white glyph on it.
calsync's glyph is a calendar page whose two rings sit on top and whose face holds the two sync arrows, centred
between the bar under the rings and the bottom of the page. Jake picked this one (849a6d7) with the arrows moved
down to that centre. The same artwork is used at every size, only scaled: no separately drawn small cut.

    python tools/build_icons.py            # write the SVGs (stdlib only)
    python tools/build_icons.py --png      # also the PNGs and favicon.ico (needs Chrome or Edge, and Pillow for the .ico)

Edit the numbers here, never the files it writes.
"""
import math
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRAND = os.path.join(ROOT, "brand")

TILE_TOP, TILE_BOTTOM = "#2c3648", "#0b0f16"     # LocalHub's tile
GLYPH_TOP, GLYPH_BOTTOM = "#ffffff", "#d9e1ec"   # LocalHub's glyph


def f(n):
    return f"{n:.1f}".rstrip("0").rstrip(".")


def arrow(cx, cy, r, a0, a1, w, head):
    """One sync arrow: an arc from angle a0 to a1 (degrees, clockwise on screen) and a filled head at a1."""
    p = lambda a, rr=r: (cx + rr * math.cos(math.radians(a)), cy + rr * math.sin(math.radians(a)))
    stop = a1 - math.degrees(head * 0.55 / r)            # the line ends inside the head, so the tip stays sharp
    x0, y0 = p(a0)
    x1, y1 = p(stop)
    large = 1 if (stop - a0) % 360 > 180 else 0
    line = f'<path d="M{f(x0)} {f(y0)}A{f(r)} {f(r)} 0 {large} 1 {f(x1)} {f(y1)}" fill="none" stroke-width="{f(w)}" stroke-linecap="round"/>'
    tx, ty = -math.sin(math.radians(a1)), math.cos(math.radians(a1))   # direction of travel at a1
    bx, by = p(a1 - math.degrees(head * 0.35 / r))
    tip = (bx + tx * head, by + ty * head)
    nx, ny = math.cos(math.radians(a1)), math.sin(math.radians(a1))
    side = head * 0.92
    a, b = (bx + nx * side, by + ny * side), (bx - nx * side, by - ny * side)
    tri = f'<path d="M{f(tip[0])} {f(tip[1])}L{f(a[0])} {f(a[1])}L{f(b[0])} {f(b[1])}Z" stroke-width="{f(w * 0.35)}" stroke-linejoin="round"/>'
    return line + tri


def glyph(small=False, cy=308):
    """The calendar and its arrows, on a 512 grid. small=True is heavier, for 16 to 32 pixels."""
    body = (120, 150, 272, 244, 56)                      # x, y, w, h, corner
    ring_w, ring_h, ring_y = (44, 86, 104) if small else (38, 84, 108)
    rings = (184 - ring_w / 2, 328 - ring_w / 2)
    cx, r = 256, (54 if small else 58)                 # cy: halfway between the bar's bottom (222) and the page's (394)
    w = 30 if small else 23
    head = 30 if small else 25
    bar = 212 if not small else 216                       # the line under the calendar's top
    parts = [f'<rect x="{body[0]}" y="{body[1]}" width="{body[2]}" height="{body[3]}" rx="{body[4]}" fill="url(#g)"/>']
    for x in rings:                                     # rings: a gap in tile colour, then the white ring on top
        parts.append(f'<rect x="{f(x - 12)}" y="{ring_y - 12}" width="{ring_w + 24}" height="{ring_h + 24}" rx="{f((ring_w + 24) / 2)}" fill="url(#t)"/>')
        parts.append(f'<rect x="{f(x)}" y="{ring_y}" width="{ring_w}" height="{ring_h}" rx="{f(ring_w / 2)}" fill="url(#g)"/>')
    cut = 'fill="url(#t)" stroke="url(#t)"'
    if not small:
        parts.append(f'<rect x="{body[0]}" y="{bar}" width="{body[2]}" height="10" fill="url(#t)"/>')
    parts.append(f'<g {cut}>' + arrow(cx, cy, r, 200, 338, w, head) + arrow(cx, cy, r, 20, 158, w, head) + '</g>')
    return "".join(parts)


def defs():
    return ('<defs><linearGradient id="t" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="512">'
            f'<stop offset="0" stop-color="{TILE_TOP}"/><stop offset="1" stop-color="{TILE_BOTTOM}"/></linearGradient>'
            '<linearGradient id="g" gradientUnits="userSpaceOnUse" x1="0" y1="104" x2="0" y2="394">'
            f'<stop offset="0" stop-color="{GLYPH_TOP}"/><stop offset="1" stop-color="{GLYPH_BOTTOM}"/></linearGradient></defs>')


def tile_svg(small=False, square=False):
    """The full app icon. square=True fills the corners, for places that round them on their own (iOS, Android)."""
    corner = 0 if square else 114
    art = glyph(small)
    if small:   # a browser tab has 16 pixels: the glyph takes more of the tile
        art = f'<g transform="translate(256 262) scale(1.16) translate(-256 -250)">{art}</g>'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="calsync">{defs()}'
            f'<rect width="512" height="512" rx="{corner}" fill="url(#t)"/>{art}</svg>')


def glyph_svg(color):
    """The glyph alone in one colour, for text, buttons and the docs. The cut-outs are see-through."""
    g = glyph().replace('fill="url(#g)"', f'fill="{color}"')
    mask = g.replace(f'fill="{color}"', 'fill="#fff"').replace('url(#t)', '#000')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="96 88 320 320" role="img" aria-label="calsync">'
            f'<defs><mask id="m" maskUnits="userSpaceOnUse" x="0" y="0" width="512" height="512">{mask}</mask></defs>'
            f'<rect x="96" y="88" width="320" height="320" fill="{color}" mask="url(#m)"/></svg>')


def glyph_dark_svg():
    """The calendar on its own, no tile: a dark page with white rings' gaps, bar and arrows, for white backgrounds.
    The same drawing as the icon with its two colours swapped, so it always matches it."""
    g = glyph().replace("url(#g)", "url(#D)").replace("url(#t)", "#fff")
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="104 80 304 330" role="img" aria-label="calsync">'
            '<defs><linearGradient id="D" gradientUnits="userSpaceOnUse" x1="0" y1="96" x2="0" y2="400">'
            f'<stop offset="0" stop-color="{TILE_TOP}"/><stop offset="1" stop-color="{TILE_BOTTOM}"/></linearGradient></defs>{g}</svg>')


INK = "#1b2230"     # calsync is white: an ink outline, rings, bar and arrows on a white page


def white_arrows():
    """The sync arrows alone, in ink, centred in the white calendar's body: from the bar's bottom (222) to the middle
    of the outline at the bottom (390), so at 306."""
    g = glyph(cy=306)
    return g[g.index('<g fill="url(#t)"'):].replace("url(#t)", INK)


def glyph_light_svg():
    """The white calendar: white page with an ink outline, ink rings, bar and sync arrows. The site's header uses it."""
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="104 80 304 330" role="img" aria-label="calsync">'
            f'<rect x="124" y="154" width="264" height="236" rx="52" fill="#fff" stroke="{INK}" stroke-width="14"/>'
            f'<rect x="153" y="96" width="62" height="108" rx="31" fill="{INK}"/><rect x="297" y="96" width="62" height="108" rx="31" fill="{INK}"/>'
            f'<rect x="120" y="212" width="272" height="10" fill="{INK}"/>{white_arrows()}</svg>')


FILES = {
    "mark.svg": lambda: tile_svg(),
    "mark-small.svg": lambda: tile_svg(),     # the same artwork as mark.svg, kept under this name for headers and tabs
    "mark-square.svg": lambda: tile_svg(square=True),
    "glyph-white.svg": lambda: glyph_svg("#ffffff"),
    "glyph-black.svg": lambda: glyph_svg("#000000"),
    "glyph-dark.svg": glyph_dark_svg,
    "glyph-light.svg": glyph_light_svg,
}

PNGS = {   # file: (source svg, size)
    "mark-1024.png": ("mark.svg", 1024),
    "icon-512.png": ("mark.svg", 512),
    "icon-192.png": ("mark.svg", 192),
    "apple-touch-icon.png": ("mark-square.svg", 180),
    "favicon-32.png": ("mark-small.svg", 32),
    "favicon-16.png": ("mark-small.svg", 16),
}

COPIES = {   # where each file goes besides brand/
    "site": ["mark.svg", "mark-small.svg", "glyph-light.svg", "icon-192.png", "icon-512.png", "apple-touch-icon.png", "favicon-32.png", "favicon.ico", "og.png"],
    "docs-site": ["mark.svg", "mark-small.svg", "apple-touch-icon.png", "favicon-32.png", "favicon.ico"],
}


def browser():
    for p in (os.environ.get("CHROME"), shutil.which("chrome"), shutil.which("google-chrome"), shutil.which("chromium"),
              r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"):
        if p and os.path.exists(p):
            return p
    sys.exit("No Chrome or Edge found. Set CHROME to its path.")


def render(svg_path, size, out_path):
    with tempfile.TemporaryDirectory() as tmp:
        page = os.path.join(tmp, "p.html")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(f'<html><body style="margin:0;background:transparent"><img src="file:///{svg_path.replace(os.sep, "/")}" width="{size}" height="{size}" style="display:block"></body></html>')
        shot = os.path.join(tmp, "s.png")
        subprocess.run([browser(), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                        "--default-background-color=00000000", f"--window-size={size},{size}", f"--screenshot={shot}",
                        f"--user-data-dir={os.path.join(tmp, 'u')}", "file:///" + page.replace(os.sep, "/")],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        shutil.copyfile(shot, out_path)


OG = """<html><head><style>
@font-face {{ font-family: Inter; src: url("file:///{fonts}/inter-600.woff2"); font-weight: 600; }}
body {{ margin: 0; width: 1200px; height: 630px; background: #fff; color: #1d1d1f; display: flex; flex-direction: column; align-items: center; justify-content: center;
  font-family: Inter, -apple-system, "Segoe UI", sans-serif; font-weight: 600; letter-spacing: -.04em; }}
.lock {{ display: flex; align-items: center; gap: 26px; font-size: 92px; letter-spacing: -.035em; }}
img {{ width: 96px; height: 104px; }}
p {{ margin: 44px 0 0; max-width: 980px; text-align: center; font-size: 60px; line-height: 1.05; }} span {{ color: #6e6e73; }}
</style></head><body><div class="lock"><img src="file:///{mark}">CalSync</div><p>All your calendars, <span>synced the way they should be.</span></p></body></html>"""


def render_og(out_path):
    """The 1200 by 630 picture links show when shared: the white calendar and CalSync, over the site's headline."""
    with tempfile.TemporaryDirectory() as tmp:
        page = os.path.join(tmp, "og.html")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(OG.format(fonts=os.path.join(ROOT, "site", "fonts").replace(os.sep, "/"), mark=os.path.join(BRAND, "glyph-light.svg").replace(os.sep, "/")))
        shot = os.path.join(tmp, "s.png")
        subprocess.run([browser(), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1", "--allow-file-access-from-files",
                        "--window-size=1200,630", f"--screenshot={shot}", f"--user-data-dir={os.path.join(tmp, 'u')}", "file:///" + page.replace(os.sep, "/")],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        shutil.copyfile(shot, out_path)


def main():
    os.makedirs(BRAND, exist_ok=True)
    for name, make in FILES.items():
        with open(os.path.join(BRAND, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(make() + "\n")
    if "--png" in sys.argv:
        for name, (src, size) in PNGS.items():
            render(os.path.join(BRAND, src), size, os.path.join(BRAND, name))
        from PIL import Image
        big = Image.open(os.path.join(BRAND, "mark-1024.png"))
        small = Image.open(os.path.join(BRAND, "favicon-32.png"))
        small.save(os.path.join(BRAND, "favicon.ico"), sizes=[(16, 16), (32, 32)], append_images=[big.resize((48, 48), Image.LANCZOS)])
        render_og(os.path.join(BRAND, "og.png"))
    for folder, names in COPIES.items():
        for name in names:
            src = os.path.join(BRAND, name)
            if os.path.exists(src):
                shutil.copyfile(src, os.path.join(ROOT, folder, name))
    print("wrote", ", ".join(sorted(os.listdir(BRAND))))


if __name__ == "__main__":
    main()
