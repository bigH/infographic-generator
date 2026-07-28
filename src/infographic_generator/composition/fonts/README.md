# Embedded fonts

Latin-subset `woff2` so a composed PNG renders identically on any machine instead of falling back
to host fonts. The composer inlines these as `data:font/woff2;base64,…` in `@font-face`; nothing
here is fetched at render time. Pulled from the Google Fonts CSS2 API, taking only the
`/* latin */` subset URL per face.

| File | Family | Weight / style | Role | Bytes | base64 |
|---|---|---|---|---|---|
| `zilla-slab-700.woff2` | Zilla Slab | 700 normal | display, big numerals | 16,788 | 22,384 |
| `pt-serif-400.woff2` | PT Serif | 400 normal | body prose | 13,400 | 17,868 |
| `pt-serif-400-italic.woff2` | PT Serif | 400 italic | body prose italic | 14,236 | 18,984 |
| `ibm-plex-mono-400.woff2` | IBM Plex Mono | 400 normal | data, small-caps ticks | 10,052 | 13,404 |

Total 54,476 bytes on disk; **72,640 base64 chars** added to every composed document.

## Licences — all SIL Open Font License 1.1

- Zilla Slab — Copyright 2017, The Mozilla Foundation → `OFL-Zilla-Slab.txt`
- PT Serif — Copyright (c) 2010, ParaType Ltd., RFN "PT Sans", "PT Serif", "ParaType" → `OFL-PT-Serif.txt`
- IBM Plex Mono — Copyright © 2017 IBM Corp. with Reserved Font Name "Plex" → `OFL-IBM-Plex-Mono.txt`

OFL requires the licence text to travel with the fonts — hence the `.txt` files. Don't rename a
family without reading its Reserved Font Name clause.

**Gotcha:** Zilla Slab's default figures are old-style (`0` is 531/1000 against a 650 cap height).
For headline numbers set `font-variant-numeric: lining-nums tabular-nums` — the subset keeps
`lnum`/`tnum` and chromium honours both.
