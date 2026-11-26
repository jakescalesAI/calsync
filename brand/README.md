# calsync mark

A calendar page with its two rings and the sync arrows, in white on LocalHub's dark slate tile. Same tile, gradients
and corner as LocalHub's icon, so the two read as one family.

Every file here is written by `tools/build_icons.py`. Change the numbers there and run it again, never the files:

```bash
python tools/build_icons.py --png    # needs Chrome or Edge for the PNGs, and Pillow for favicon.ico
```

| File | Use |
|---|---|
| `mark.svg` | The app icon: tile with rounded corners. The site, the docs, README. 48 px and up. |
| `mark-small.svg` | The same with a bigger, heavier glyph for 16 to 32 px. Browser tabs and the setup page. |
| `mark-square.svg` | Square corners, for systems that round icons themselves (iOS, Android). |
| `glyph-white.svg` / `glyph-black.svg` | The calendar alone, one colour, see-through cut-outs. On photos or next to text. |
| `mark-1024.png`, `icon-512.png`, `icon-192.png` | PNG exports of `mark.svg`. |
| `apple-touch-icon.png` | 180 px from `mark-square.svg`. |
| `favicon-32.png`, `favicon-16.png`, `favicon.ico` | Browser tabs, from `mark-small.svg`. |
| `og.png` | 1200 by 630, the wordmark with the tile in it. The picture a shared link shows. |

The script also copies what each site needs into `site/` and `docs-site/`. The setup page carries `mark-small.svg`
inside `calsync_web.py`, so it needs no outside files.
