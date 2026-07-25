// Headless responsive audit for the ASTRA dashboard.
//
// Uses Playwright device emulation (true viewport + device-pixel-ratio + touch +
// mobile UA) — the same technique QA/UX teams use, not browser zoom. Captures a
// full-page screenshot per device and flags the two most common mobile defects:
//   1. horizontal overflow  (any element extending past the viewport right edge)
//   2. under-sized tap targets (< 44px tall, per Apple/Google touch guidance)
//
// Usage:
//   npm install            # once, pulls Playwright (browsers via system Chrome)
//   npm run audit                       # audits the deployed dashboard (public view)
//   URL=http://localhost:8000 npm run audit   # audit a local server
//   PW='...' npm run audit              # also unlock the owner/private view
//
// PW (optional) signs in via the dashboard's Supabase login so RLS-gated
// panels (paper portfolio, autotrader, drawers) render. Read from env only —
// never hardcode or commit a password.
//
// Screenshots land in ./shots (gitignored). JSON report prints to stdout.

import { chromium, devices } from 'playwright';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const URL = process.env.URL || 'https://abhi-kirk.github.io/Astra/';
const OUT = path.join(__dirname, 'shots');
fs.mkdirSync(OUT, { recursive: true });

const targets = [
  { name: 'iphone-se',        device: devices['iPhone SE'] },          // 375w — smallest common phone
  { name: 'iphone-14-promax', device: devices['iPhone 14 Pro Max'] },  // 430w — large phone
  { name: 'pixel-7',          device: devices['Pixel 7'] },            // 412w — Android
  { name: 'ipad-mini',        device: devices['iPad Mini'] },          // 768w — tablet breakpoint
  { name: 'desktop-1440',     device: { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, isMobile: false } },
];

// Elements whose right edge passes the viewport edge -> horizontal scroll.
const overflowProbe = () => {
  const vw = document.documentElement.clientWidth;
  const bad = [];
  const walk = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.right > vw + 1) {
      const cs = getComputedStyle(el);
      bad.push({
        tag: el.tagName.toLowerCase(),
        id: el.id || null,
        cls: (el.className && el.className.toString().slice(0, 60)) || null,
        right: Math.round(r.right),
        width: Math.round(r.width),
        overflowX: cs.overflowX,
      });
    }
    for (const c of el.children) walk(c);
  };
  walk(document.body);
  return {
    viewportWidth: vw,
    docScrollWidth: document.documentElement.scrollWidth,
    horizontalScroll: document.documentElement.scrollWidth > vw + 1,
    offenders: bad.slice(0, 40),
  };
};

const tapTargetProbe = () => {
  const sel = 'button, a, input, [role=button]';
  const out = [];
  document.querySelectorAll(sel).forEach((el) => {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && r.height < 44) {
      out.push({
        t: el.tagName.toLowerCase(),
        id: el.id || null,
        cls: (el.className || '').toString().slice(0, 40),
        w: Math.round(r.width),
        h: Math.round(r.height),
        txt: (el.textContent || '').trim().slice(0, 20),
      });
    }
  });
  return out.slice(0, 30);
};

const results = {};
const browser = await chromium.launch({ channel: 'chrome' });

for (const t of targets) {
  const ctx = await browser.newContext({ ...t.device });
  const page = await ctx.newPage();
  await page.goto(URL, { waitUntil: 'networkidle', timeout: 45000 });
  if (process.env.PW) {
    // Unlock the owner/private view via the dashboard's own Supabase login.
    await page.evaluate(() => document.getElementById('login-overlay').classList.remove('hidden'));
    await page.fill('#login-password', process.env.PW);
    await page.click('#login-submit');
    await page.waitForTimeout(3500); // auth round-trip + RLS-gated re-render
  }
  await page.waitForTimeout(2500); // let Supabase data + Chart.js render
  await page.screenshot({ path: path.join(OUT, `${t.name}.png`), fullPage: true });
  results[t.name] = await page.evaluate(overflowProbe);
  results[t.name].smallTapTargets = await page.evaluate(tapTargetProbe);
  await ctx.close();
}

await browser.close();
console.log(JSON.stringify(results, null, 2));
