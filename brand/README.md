# CalSync mark

A white calendar page with its two rings and circular sync arrows, on LocalHub's dark slate tile. The arrows sit
halfway between the bar under the rings and the bottom of the page. Same tile, gradients and corner as LocalHub's
icon, so the two read as one family. Jake picked it on 11-26 (first drawn in 849a6d7, arrows centred since).

Every file here is written by `tools/build_icons.py`. Change the numbers there and run it again, never the files:

```bash
python tools/build_icons.py --png    # needs Chrome or Edge for the PNGs, and Pillow for favicon.ico
```

One icon at every size: the small files are the same artwork, only scaled.

| File | Use |
|---|---|
| `mark.svg` | The app icon: tile with rounded corners. The site, the docs, README. |
| `mark-small.svg` | The same artwork as `mark.svg`, under the name the headers, tabs and setup page use. |
| `mark-square.svg` | Square corners, for systems that round icons themselves (iOS, Android). |
| `glyph-white.svg` / `glyph-black.svg` | The calendar alone, one colour, see-through arrows. On photos or next to text. |
| `glyph-light.svg` | The white calendar: white page with an ink outline, solid ink rings, bar and arrows. The site's header logo; the intro draws the same shapes and docks onto it. |
| `mark-1024.png`, `icon-512.png`, `icon-192.png` | PNG exports of `mark.svg`. |
| `apple-touch-icon.png` | 180 px from `mark-square.svg`. |
| `favicon-32.png`, `favicon-16.png`, `favicon.ico` | Browser tabs, the same artwork scaled down. |
| `og.png` | 1200 by 630, the wordmark with the tile in it. The picture a shared link shows. |

The script also copies what each site needs into `site/` and `docs-site/`. The setup page carries the mark inside
`calsync_web.py`, so it needs no outside files.
