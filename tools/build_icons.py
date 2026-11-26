#!/usr/bin/env python3
"""Draw calsync's mark and write every size the app, the website and the docs use.

The mark is calsync's first one, the calendar page with its rings, the sync arrows and 31, on LocalHub's tile:
a rounded square with a dark slate gradient, the page in LocalHub's white-to-blue-grey. The arrows sit clear of the
rings, and every line is one weight. The same artwork is used at every size, only scaled: Jake approved one icon,
so there is no separately drawn small cut.

    python tools/build_icons.py            # write the SVGs (stdlib only)
    python tools/build_icons.py --png      # also the PNGs and favicon.ico (needs Chrome or Edge, and Pillow for the .ico)

Edit the numbers here, never the files it writes.
"""
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


INK = "#0b0f16"          # the tile's darkest colour: the arrows and the 31
STROKE = 20              # every drawn line, in 512 units

# The 31 from calsync's first mark (Inter SemiBold as outlines, on its 64 grid, centred near 31.8, 38.6).
THIRTY_ONE = ("M25.78 50.67Q23.3 50.67 21.39 49.81Q19.47 48.96 18.37 47.46Q17.26 45.95 17.2 43.98L21.44 43.98Q21.5 44.94 22.08 45.65"
              "Q22.66 46.36 23.62 46.75Q24.58 47.13 25.78 47.13Q27.04 47.13 28.01 46.69Q28.98 46.25 29.54 45.45Q30.1 44.66 30.1 43.62"
              "Q30.1 42.54 29.53 41.72Q28.96 40.9 27.91 40.44Q26.85 39.97 25.35 39.97L23.27 39.97L23.27 36.72L25.35 36.72Q26.58 36.72 27.51 36.29"
              "Q28.44 35.86 28.96 35.08Q29.49 34.31 29.49 33.28Q29.49 32.29 29.04 31.55Q28.59 30.81 27.76 30.39Q26.93 29.97 25.83 29.97"
              "Q24.74 29.97 23.82 30.36Q22.91 30.75 22.34 31.46Q21.77 32.17 21.74 33.17L17.69 33.17Q17.72 31.2 18.82 29.71Q19.91 28.22 21.75 27.38"
              "Q23.59 26.53 25.86 26.53Q28.21 26.53 29.94 27.42Q31.68 28.3 32.63 29.78Q33.59 31.25 33.59 33.03Q33.59 35.02 32.42 36.36"
              "Q31.25 37.7 29.34 38.13L29.34 38.32Q30.98 38.54 32.12 39.29Q33.25 40.04 33.85 41.2Q34.44 42.37 34.44 43.84Q34.44 45.81 33.32 47.35"
              "Q32.2 48.9 30.25 49.78Q28.3 50.67 25.78 50.67ZM46.4 26.85L46.4 50.35L42.22 50.35L42.22 30.6L42.06 30.6L36.58 34.55L36.58 30.54L41.73 26.85Z")


def glyph(small=False):
    """The calendar: page, rings, top bar, the two sync arrows lower down, and the 31 between them."""
    w = 26 if small else STROKE
    page = (112, 140, 288, 276, 56)                   # x, y, width, height, corner
    rings = (184, 328)
    out = [f'<rect x="{page[0]}" y="{page[1]}" width="{page[2]}" height="{page[3]}" rx="{page[4]}" fill="url(#g)"/>']
    for cx in rings:                                   # each ring: a gap in the tile colour, then a white pill
        out.append(f'<rect x="{cx - 30}" y="{88}" width="60" height="104" rx="30" fill="url(#t)"/>')
        out.append(f'<rect x="{cx - 18}" y="{100}" width="36" height="80" rx="18" fill="url(#g)"/>')
    # the original sync loop at its original size, lower: it now starts well under the rings instead of touching them
    cx, cy, left, right, top, r = 256, 309, 154, 358, 224, 46
    arrow = (f'M{left} {cy - 4}V{top + r}A{r} {r} 0 0 1 {left + r} {top}H{right - r}A{r} {r} 0 0 1 {right} {top + r}V{cy + 2}'
             f'M{right - 22} {cy - 20}L{right} {cy + 2}L{right + 22} {cy - 20}')
    out.append(f'<g fill="none" stroke="{INK}" stroke-width="{w}" stroke-linecap="round" stroke-linejoin="round">'
               f'<path d="{arrow}"/><path transform="rotate(180 {cx} {cy})" d="{arrow}"/></g>')
    if not small:                                      # the 31 stays on the big icon; at 32 px and below it is mush
        out.append(f'<path transform="translate({cx} {cy}) scale(4.4) translate(-31.8 -38.6)" d="{THIRTY_ONE}" fill="{INK}" stroke="{INK}" stroke-width="0.6" stroke-linejoin="round"/>')
    return "".join(out)


def defs():
    return ('<defs><linearGradient id="t" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="512">'
            f'<stop offset="0" stop-color="{TILE_TOP}"/><stop offset="1" stop-color="{TILE_BOTTOM}"/></linearGradient>'
            '<linearGradient id="g" gradientUnits="userSpaceOnUse" x1="0" y1="96" x2="0" y2="420">'
            f'<stop offset="0" stop-color="{GLYPH_TOP}"/><stop offset="1" stop-color="{GLYPH_BOTTOM}"/></linearGradient></defs>')


def tile_svg(small=False, square=False):
    """The full app icon. square=True fills the corners, for places that round them on their own (iOS, Android)."""
    corner = 0 if square else 114
    art = glyph(small)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="calsync">{defs()}'
            f'<rect width="512" height="512" rx="{corner}" fill="url(#t)"/>{art}</svg>')


def glyph_svg(color):
    """The glyph alone in one colour, for text, buttons and the docs. The cut-outs are see-through."""
    g = glyph().replace('fill="url(#g)"', f'fill="{color}"')
    mask = g.replace(f'fill="{color}"', 'fill="#fff"').replace('url(#t)', '#000').replace(INK, "#000")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="96 88 320 320" role="img" aria-label="calsync">'
            f'<defs><mask id="m" maskUnits="userSpaceOnUse" x="0" y="0" width="512" height="512">{mask}</mask></defs>'
            f'<rect x="96" y="88" width="320" height="320" fill="{color}" mask="url(#m)"/></svg>')


FILES = {
    "mark.svg": lambda: tile_svg(),
    "mark-small.svg": lambda: tile_svg(),     # the same artwork as mark.svg: Jake wants one icon, only ever scaled
    "mark-square.svg": lambda: tile_svg(square=True),
    "glyph-white.svg": lambda: glyph_svg("#ffffff"),
    "glyph-black.svg": lambda: glyph_svg("#000000"),
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
    "site": ["mark.svg", "mark-small.svg", "icon-192.png", "icon-512.png", "apple-touch-icon.png", "favicon-32.png", "favicon.ico", "og.png"],
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
  font-family: Inter, -apple-system, "Segoe UI", sans-serif; font-weight: 600; letter-spacing: -.05em;
  background-image: radial-gradient(700px 260px at 50% 118%, rgba(0,113,227,.12), rgba(94,92,230,.04) 60%, transparent); }}
h1 {{ margin: 0; font-size: 168px; line-height: 1.06; white-space: nowrap; }}
img {{ display: inline-block; width: .86em; height: .86em; margin: 0 .05em 0 .08em; vertical-align: -.1em; border-radius: 22.5%; box-shadow: 0 24px 48px -16px rgba(11,15,22,.45); }}
p {{ margin: 18px 0 0; font-size: 54px; letter-spacing: -.035em; }} span {{ color: #6e6e73; }}
</style></head><body><h1>cal<img src="file:///{mark}">sync</h1><p>Every calendar. <span>Always in sync.</span></p></body></html>"""


def render_og(out_path):
    """The 1200 by 630 picture links show when shared: the wordmark with the tile in it, as on the site."""
    with tempfile.TemporaryDirectory() as tmp:
        page = os.path.join(tmp, "og.html")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(OG.format(fonts=os.path.join(ROOT, "site", "fonts").replace(os.sep, "/"), mark=os.path.join(BRAND, "mark.svg").replace(os.sep, "/")))
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
