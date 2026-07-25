# Responsive audit

Headless mobile/responsive regression check for the ASTRA dashboard. Uses Playwright
**device emulation** (true viewport + device-pixel-ratio + touch + mobile UA — not browser
zoom) to screenshot the dashboard at real device profiles and auto-flag the two most common
mobile defects:

1. **Horizontal overflow** — any element extending past the viewport edge (causes page-level
   sideways scroll / clipped content).
2. **Under-sized tap targets** — interactive elements under the 44px Apple/Google minimum.

## Setup

```bash
cd dashboard/tools/responsive-audit
npm install          # pulls Playwright; browsers run via system Chrome (channel: 'chrome')
```

## Run

```bash
npm run audit                                   # deployed dashboard, public view
URL=http://localhost:8000 npm run audit         # a local server (e.g. `python3 -m http.server`)
PW='...' npm run audit                           # also unlock the owner/private view
```

- `URL` (optional) — target to audit. Defaults to the deployed GitHub Pages site.
- `PW` (optional) — owner password. Signs in via the dashboard's own Supabase login so
  RLS-gated panels (paper portfolio, autotrader, drawers) render. **Read from env only —
  never hardcode or commit a password.**

Full-page screenshots land in `shots/` (gitignored); a JSON report prints to stdout. A device
passes when it reports `horizontalScroll: false`. Rows that overflow *inside* a scroll-box
(e.g. `.paper-rows`) still show as element-level offenders but do not scroll the page — the
authoritative signal is the page-level `horizontalScroll` flag.

Devices covered: iPhone SE (320), iPhone 14 Pro Max (430), Pixel 7 (412), iPad Mini (768),
desktop 1440 (baseline). Edit the `targets` array in `audit.mjs` to add more.
