# calsync mark

A sync ring around a watch face showing 31: every calendar, always on time. Black, white and grey only.

| File | Use |
|---|---|
| `mark-white.svg` / `mark-black.svg` | Full mark (ticks, crown, 31), no panel. 48 px and up. |
| `mark-small-white.svg` / `mark-small-black.svg` | No ticks, heavier lines, bigger 31. Under 48 px: site header, footers. |
| `panel-dark.svg` / `panel-light.svg` | Full mark on its own tile. App icons, social images. |
| `panel-dark-small.svg` | Small mark on the dark tile. Browser favicon. |
| `*-1024.png`, `panel-dark-512.png` | PNG exports, transparent outside the tile. |

The 31 is drawn as strokes, not set in a font, so the files need nothing else to render.
