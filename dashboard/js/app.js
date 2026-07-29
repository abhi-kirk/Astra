// ============================================================
// ASTRA — Dashboard App
// ============================================================

const SUPABASE_URL  = 'https://irnpxdnuldvawhbjscmc.supabase.co';
const SUPABASE_ANON = 'sb_publishable_sSt4OK7825qQ-DZqf8o_Ow_JGbV2G7G';

// ── Auth state ────────────────────────────────────────────────
// Private data (avg cost, P&L, advisor note) only fetched after Supabase Auth.
// Gate is enforced server-side via RLS — private data never reaches the browser
// for unauthenticated visitors, even if they inspect the source.
let isAuthenticated = false;

// Convictions data cached at startup for modal lookups (theme, status, per-ticker notes)
let _convictions = null;

const AUTH_EMAIL = 'abhikirk@icloud.com';

function updateLockBtn(authed) {
  const btn   = document.getElementById('lock-btn');
  const icon  = document.getElementById('lock-icon');
  const label = document.getElementById('lock-label');
  btn.classList.toggle('unlocked', authed);
  icon.textContent  = authed ? '🔓' : '🔒';
  label.textContent = authed ? 'SIGNED IN' : 'PRIVATE';
}

function showLoginModal() {
  document.getElementById('login-overlay').classList.remove('hidden');
  document.getElementById('login-password').focus();
}

function closeLoginModal() {
  document.getElementById('login-overlay').classList.add('hidden');
  document.getElementById('login-password').value = '';
  document.getElementById('login-error').classList.add('hidden');
}

function initLockButton(sb) {
  const btn = document.getElementById('lock-btn');
  btn.addEventListener('click', async () => {
    if (isAuthenticated) {
      await sb.auth.signOut();
      location.reload();
    } else {
      showLoginModal();
    }
  });

  document.getElementById('login-close').addEventListener('click', closeLoginModal);
  document.getElementById('login-overlay').addEventListener('click', e => {
    if (e.target === document.getElementById('login-overlay')) closeLoginModal();
  });

  const submitBtn = document.getElementById('login-submit');
  const passwordEl = document.getElementById('login-password');
  const errorEl = document.getElementById('login-error');

  async function attemptLogin() {
    const password = passwordEl.value.trim();
    if (!password) return;
    submitBtn.disabled = true;
    submitBtn.textContent = 'VERIFYING…';
    errorEl.classList.add('hidden');

    const { error } = await sb.auth.signInWithPassword({
      email: AUTH_EMAIL,
      password,
    });

    if (error) {
      errorEl.textContent = 'Incorrect password.';
      errorEl.classList.remove('hidden');
      submitBtn.disabled = false;
      submitBtn.textContent = 'UNLOCK →';
      passwordEl.value = '';
      passwordEl.focus();
    } else {
      location.reload();
    }
  }

  submitBtn.addEventListener('click', attemptLogin);
  passwordEl.addEventListener('keydown', e => { if (e.key === 'Enter') attemptLogin(); });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeLoginModal();
  });
}

// CDN exports supabase-js differently depending on version — handle both
function createSupabaseClient() {
  const lib = window.supabase;
  if (!lib) throw new Error('Supabase CDN script failed to load');
  const factory = lib.createClient ?? lib.default?.createClient;
  if (!factory) throw new Error('createClient not found in Supabase CDN export');
  return factory(SUPABASE_URL, SUPABASE_ANON);
}

// Theme → tickers map (drives swim lanes). Rebuilt from convictions DB on load + after saves.
let THEME_MAP = {
  space:        { label: 'Space',        conviction: 'very_high', tickers: ['RKLB','ASTS','ARKX','SPCX','NASA','SPCE','SMR'] },
  core_tech:    { label: 'Core Tech',    conviction: 'high',      tickers: ['NVDA','GOOGL','AMZN','AAPL','MSFT','AMD','CRM','NFLX','CRSR','BB'] },
  ev_transition:{ label: 'EV',          conviction: 'high',      tickers: ['NIO','BYDDY','LCID','CHPT'] },
  cannabis:     { label: 'Cannabis',     conviction: 'low',       tickers: ['CRON','SNDL','VFF'] },
  other:        { label: 'Other',        conviction: null,        tickers: [] },
};

// Stored render data so lanes can be re-rendered after conviction changes
let _lastMarketData = {}, _lastSignalsByTicker = {}, _lastDecisionsByTicker = {}, _lastPaperByTicker = {};

function buildThemeMapFromConvictions(convictions) {
  const map = {};
  for (const [key, theme] of Object.entries(convictions.themes || {})) {
    const tickers = [
      ...(theme.approved   || []),
      ...(theme.preferred  || []),
      ...(theme.hold_only  || []),
      ...(theme.do_not_add || []),
    ];
    const label = key.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
    map[key] = { label, conviction: theme.conviction || 'medium', tickers };
  }
  map.other = { label: 'Other', conviction: null, tickers: [] };
  return map;
}

// ── Ticker linkification ──────────────────────────────────────

function linkifyText(html, allTickers) {
  if (!allTickers.length) return html;
  // Sort longest first to avoid partial matches (e.g. ARKX before ARK)
  const sorted = [...allTickers].sort((a, b) => b.length - a.length);
  const pattern = new RegExp(`\\b(${sorted.join('|')})\\b`, 'g');
  return html.replace(pattern, '<span class="ticker-link" data-ticker="$1">$1</span>');
}

function scrollToCard(ticker) {
  const card = document.querySelector(`.pos-card[data-ticker="${ticker}"]`);
  if (!card) return;
  card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  card.classList.remove('card-flash');
  void card.offsetWidth; // reflow to restart animation
  card.classList.add('card-flash');
  setTimeout(() => card.classList.remove('card-flash'), 1400);
}

// Delegate ticker-link clicks from any container
document.addEventListener('click', e => {
  const el = e.target.closest('.ticker-link, .ticker-link-plain, .treemap-tile');
  if (el) {
    e.preventDefault();
    scrollToCard(el.dataset.ticker);
  }
});

// ── Reason text cleanup ───────────────────────────────────────

function cleanReason(r) {
  return r
    .replace(/^DO NOT ADD:\s*DO NOT ADD\.\s*/i, '')
    .replace(/^DO NOT ADD:\s*/i, '')
    .replace(/\s*[✓✗]\s*$/, '')
    .trim();
}

// ── Starfield ──────────────────────────────────────────────

function initStarfield() {
  const canvas = document.getElementById('starfield');
  const ctx = canvas.getContext('2d');
  let stars = [];

  function resize() {
    canvas.width  = window.innerWidth;
    canvas.height = window.innerHeight;
  }

  function createStars(n) {
    stars = Array.from({ length: n }, () => ({
      x:     Math.random() * canvas.width,
      y:     Math.random() * canvas.height,
      r:     Math.random() * 1.2 + 0.2,
      alpha: Math.random() * 0.6 + 0.1,
      speed: Math.random() * 0.03 + 0.005,
      drift: (Math.random() - 0.5) * 0.02,
    }));
  }

  function draw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    stars.forEach(s => {
      s.y += s.speed;
      s.x += s.drift;
      if (s.y > canvas.height) { s.y = 0; s.x = Math.random() * canvas.width; }
      ctx.beginPath();
      ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(200, 220, 255, ${s.alpha})`;
      ctx.fill();
    });
    requestAnimationFrame(draw);
  }

  resize();
  createStars(160);
  draw();
  window.addEventListener('resize', () => { resize(); createStars(160); });
}

// ── Helpers ─────────────────────────────────────────────────

function fmt(val, decimals = 2) {
  if (val == null || isNaN(val)) return '—';
  return Number(val).toFixed(decimals);
}

function fmtPct(val) {
  if (val == null || isNaN(val)) return '—';
  const n = Number(val);
  return (n >= 0 ? '+' : '') + n.toFixed(1) + '%';
}

function fmtPrice(val) {
  if (val == null || isNaN(val)) return '—';
  return '$' + Number(val).toFixed(2);
}

function fmtBigNum(val) {
  if (val == null || isNaN(val)) return '—';
  const n = Number(val);
  const sign = n < 0 ? '-' : '';
  const abs = Math.abs(n);
  if (abs >= 1e12) return sign + '$' + (abs / 1e12).toFixed(1) + 'T';
  if (abs >= 1e9)  return sign + '$' + (abs / 1e9).toFixed(1) + 'B';
  if (abs >= 1e6)  return sign + '$' + (abs / 1e6).toFixed(1) + 'M';
  return sign + '$' + abs.toFixed(0);
}

function nextWeekdayRun() {
  // Returns a short string like "Mon Jun 30 6am" for the next scheduled run.
  // The daily analysis fires weekdays at 6:00am PT (pg_cron '0 13 * * 1-5').
  // Compute in PT wall-clock time so the label is correct regardless of the
  // viewer's timezone, and show today's run when it hasn't fired yet.
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/Los_Angeles',
      year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', hour12: false,
    }).formatToParts(new Date()).map(p => [p.type, p.value])
  );
  const hour = +parts.hour % 24;  // hour12:false yields "24" at midnight in some engines
  // Represent the PT civil date in UTC so weekday math is timezone-free.
  const run = new Date(Date.UTC(+parts.year, +parts.month - 1, +parts.day));
  // If today's 6am PT run has already fired, advance to the next day.
  if (hour >= 6) run.setUTCDate(run.getUTCDate() + 1);
  // Skip weekends.
  while ([0, 6].includes(run.getUTCDay())) run.setUTCDate(run.getUTCDate() + 1);
  return run.toLocaleDateString('en-US', {
    timeZone: 'UTC', weekday: 'short', month: 'short', day: 'numeric',
  }) + ' 6am';
}

function dataAgeText(runDateObj) {
  const ageMs = Date.now() - runDateObj.getTime();
  const ageH  = ageMs / 3600000;
  if (ageH < 1)  return 'Just now';
  if (ageH < 24) return `${Math.floor(ageH)}h ago`;
  return `${Math.floor(ageH / 24)}d ago`;
}

function rsiClass(rsi) {
  if (rsi == null) return 'neutral';
  if (rsi < 40)  return 'oversold';
  if (rsi > 60)  return 'overbought';
  return 'neutral';
}

function rsiLabel(rsi) {
  if (rsi == null) return '—';
  if (rsi < 30)  return 'DEEPLY OVERSOLD';
  if (rsi < 40)  return 'OVERSOLD';
  if (rsi > 70)  return 'OVERBOUGHT';
  if (rsi > 60)  return 'ELEVATED';
  return 'NEUTRAL';
}

function rsiLabelColor(rsi) {
  if (rsi == null) return 'var(--text-muted)';
  if (rsi < 40)  return 'var(--accent-green)';
  if (rsi > 60)  return 'var(--accent-red)';
  return 'var(--text-secondary)';
}

function tickerTheme(ticker) {
  for (const [key, cfg] of Object.entries(THEME_MAP)) {
    if (key === 'other') continue;
    if (cfg.tickers.includes(ticker)) return key;
  }
  return 'other';
}

// ── Data fetching ────────────────────────────────────────────

async function fetchLatestRunSummary(sb) {
  const { data, error } = await sb
    .from('run_summaries')
    .select('*')
    .order('id', { ascending: false })
    .limit(1)
    .single();
  if (error) throw new Error(`run_summaries query failed: ${error.message}`);
  return data;
}

async function fetchLatestDecisions(sb, runDate) {
  const dateStr = runDate.slice(0, 10);
  const { data, error } = await sb
    .from('decisions')
    .select('*')
    .gte('run_date', dateStr)
    .lt('run_date', dateStr + 'T23:59:59')
    .order('run_date', { ascending: false });
  if (error) throw new Error(`decisions query failed: ${error.message}`);
  return data || [];
}

async function fetchOpenPaperTrades(sb) {
  const { data, error } = await sb
    .from('paper_trades')
    .select('*')
    .eq('is_open', true)
    .order('run_date', { ascending: true });
  if (error) console.warn('paper_trades fetch failed:', error.message);
  return data || [];
}

async function fetchClosedPaperTrades(sb) {
  const since = new Date();
  since.setDate(since.getDate() - 90);
  const { data } = await sb
    .from('paper_trades')
    .select('*')
    .eq('is_open', false)
    .gte('run_date', since.toISOString())
    .order('closed_at', { ascending: false })
    .limit(30);
  return data || [];
}

// ── Autotrader: autonomous agentic trading (owner-only) ──
async function fetchAgentControl(sb) {
  const { data } = await sb.from('agent_control').select('*').eq('id', 1).maybeSingle();
  return data || { paused: false, halted: false, halt_reason: null };
}

async function fetchAgentTrades(sb) {
  const { data } = await sb.from('agent_trades').select('*')
    .order('run_date', { ascending: false }).limit(50);
  return data || [];
}

async function fetchAgentSnapshot(sb) {
  const { data } = await sb.from('agent_account_snapshots').select('*')
    .order('snapshot_time', { ascending: false }).limit(1).maybeSingle();
  return data || null;
}

// ── Contextual layer: latest run's Playbook proposals (owner-only) ──
// Grab the most recent rows and keep only the latest run's, so the panel reflects
// exactly what the last run's contextual layer proposed (quant + LLM, applied vs vetoed).
async function fetchContextualInputs(sb) {
  const { data, error } = await sb.from('contextual_inputs').select('*')
    .order('run_date', { ascending: false }).order('id', { ascending: false }).limit(60);
  if (error) { console.warn('contextual_inputs fetch failed:', error.message); return []; }
  if (!data || !data.length) return [];
  const latest = data[0].run_date;
  return data.filter(r => r.run_date === latest);
}

// Render the owner-only "what the contextual layer did this run" panel. Groups the latest
// run's proposals by play; each row shows the bounded delta, quant-vs-LLM source, whether it
// survived the code clamps + hard invariants (applied), and the cited rationale.
function renderPlaybookPanel(proposals) {
  const section = document.getElementById('playbook-panel');
  if (!section) return;
  section.classList.remove('hidden');

  const kindLabel = (kind, value) => {
    const v = Number(value);
    if (kind === 'size_multiplier') return { txt: `×${v.toFixed(2)}`, cls: v >= 1 ? 'pb-up' : 'pb-down' };
    if (kind === 'conviction_nudge') return { txt: `${v >= 0 ? '+' : ''}${v.toFixed(2)} C`, cls: v >= 0 ? 'pb-up' : 'pb-down' };
    if (kind === 'thesis_flag')     return { txt: `⚠ ${v.toFixed(2)}`, cls: 'pb-down' };
    return { txt: `${v}`, cls: '' };
  };
  const kindName = { size_multiplier: 'size', conviction_nudge: 'conviction', thesis_flag: 'thesis' };

  const runDate = proposals[0]?.run_date;
  const runStr  = runDate ? new Date(runDate).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '—';

  let body;
  if (!proposals.length) {
    body = `<div class="treemap-empty">No plays fired last run — the deterministic brain baseline was unchanged.</div>`;
  } else {
    // Group by play, preserving first-seen order.
    const byPlay = {};
    proposals.forEach(p => { (byPlay[p.play] = byPlay[p.play] || []).push(p); });

    body = Object.entries(byPlay).map(([play, rows]) => {
      const appliedN = rows.filter(r => r.applied).length;
      const cards = rows.map(r => {
        const k = kindLabel(r.kind, r.value);
        const srcCls = r.source === 'llm' ? 'pb-src-llm' : 'pb-src-quant';
        const appCls = r.applied ? 'pb-applied' : 'pb-vetoed';
        const appTxt = r.applied ? 'APPLIED' : 'VETOED';
        const rationale = DOMPurify.sanitize(r.rationale || '');
        return `<div class="pb-prop">
          <div class="pb-prop-head">
            <span class="ticker-link-plain pb-prop-ticker" data-ticker="${r.ticker}">${r.ticker}</span>
            <span class="pb-kind">${kindName[r.kind] || r.kind}</span>
            <span class="pb-value ${k.cls}">${k.txt}</span>
            <span class="pb-src ${srcCls}">${(r.source || '').toUpperCase()}</span>
            <span class="pb-app ${appCls}">${appTxt}</span>
          </div>
          ${rationale ? `<div class="pb-prop-rationale">${rationale}</div>` : ''}
        </div>`;
      }).join('');
      return `<div class="pb-play">
        <div class="pb-play-head">
          <span class="pb-play-name">${play.replace(/_/g, ' ')}</span>
          <span class="pb-play-count">${appliedN}/${rows.length} applied</span>
        </div>
        ${cards}
      </div>`;
    }).join('');
  }

  section.innerHTML = `
    <div class="section-label">
      <span class="section-icon">♟</span>
      <span>PLAYBOOK · CONTEXTUAL LAYER</span>
      <span class="section-badge">LLM-IN-LOOP · ${runStr}</span>
    </div>
    <div class="paper-card pb-card">
      <div class="pb-intro">Bounded, play-tagged proposals the contextual layer fed into the deterministic brain last run. The LLM never decides — every proposal is clamped and re-checked against the hard rules in code (<span class="pb-vetoed-inline">vetoed</span> = it didn't survive).</div>
      ${body}
    </div>
  `;
  // Ticker links (.ticker-link-plain) scroll to the fleet card via the global delegated handler.
}

// ── Performance charts (NAV vs SPY) + holdings treemap ───────────
// Backend (migration 014) stores a truthful nav_index per run for each track and
// raw SPY closes; the frontend only rebases the SPY line and draws. No math in JS
// beyond that rebase. See docs/performance_charts.md.

async function fetchPaperEquity(sb) {
  const { data } = await sb.from('paper_equity')
    .select('snapshot_time, nav_index')
    .order('snapshot_time', { ascending: true });
  return data || [];
}

async function fetchBenchmarkPrices(sb, symbol = 'SPY') {
  const { data } = await sb.from('benchmark_prices')
    .select('price_date, close')
    .eq('symbol', symbol)
    .order('price_date', { ascending: true });
  return data || [];
}

async function fetchAgentNavSeries(sb) {
  const { data } = await sb.from('agent_account_snapshots')
    .select('snapshot_time, nav_index')
    .order('snapshot_time', { ascending: true });
  return data || [];
}

const _localDate = ts => new Date(ts).toLocaleDateString('en-CA'); // YYYY-MM-DD local

// Rebase SPY to 100 at the track's first snapshot date; align by date (forward-fill).
// Returns null when there aren't ≥2 NAV points yet (chart shows a placeholder).
function buildNavVsSpy(navRows, spyRows) {
  const nav = (navRows || []).filter(r => r.nav_index != null);
  if (nav.length < 2) return null;
  const spy = (spyRows || [])
    .map(r => ({ date: String(r.price_date).slice(0, 10), close: Number(r.close) }))
    .filter(r => !isNaN(r.close))
    .sort((a, b) => a.date.localeCompare(b.date));
  const spyOnOrBefore = d => {
    let val = null;
    for (const s of spy) { if (s.date <= d) val = s.close; else break; }
    return val;
  };
  const firstDate = _localDate(nav[0].snapshot_time);
  const spyBase = spyOnOrBefore(firstDate) ?? (spy[0] ? spy[0].close : null);
  const labels = [], astra = [], bench = [];
  for (const r of nav) {
    const d = _localDate(r.snapshot_time);
    const c = spyOnOrBefore(d);
    labels.push(d);
    astra.push(Number(r.nav_index));
    bench.push(spyBase && c ? c / spyBase * 100 : null);
  }
  // Since-inception return (%) and outperformance vs SPY, in index points.
  const lastAstra = astra[astra.length - 1];
  const lastBench = [...bench].reverse().find(v => v != null);
  return {
    labels, astra, bench,
    sinceInception: lastAstra - 100,
    vsSpy: lastBench != null ? lastAstra - lastBench : null,
  };
}

function perfPlaceholder() {
  return `<div class="perf-empty"><span class="perf-empty-icon">◷</span>
    <span>Collecting data — the performance chart appears once there are two daily runs.</span></div>`;
}

function renderEquityCurve(canvas, series, label) {
  if (!canvas || !series || typeof Chart === 'undefined') return;
  const cs = getComputedStyle(document.documentElement);
  const v = (name, fb) => (cs.getPropertyValue(name).trim() || fb);
  const cyan = v('--accent-cyan', '#38bdf8');
  const muted = v('--text-muted', '#5a7a94');
  const textSec = v('--text-secondary', '#a8bdd0');
  const mono = 'JetBrains Mono, monospace';
  const existing = Chart.getChart(canvas);
  if (existing) existing.destroy();
  const fmtDay = d => { const p = d.split('-'); return `${p[1]}/${p[2]}`; };
  new Chart(canvas, {
    type: 'line',
    data: {
      labels: series.labels.map(fmtDay),
      datasets: [
        { label: label || 'ASTRA', data: series.astra, borderColor: cyan,
          backgroundColor: 'rgba(56,189,248,0.08)', borderWidth: 2, fill: true,
          tension: 0.25, pointRadius: 0, pointHoverRadius: 4, pointHoverBackgroundColor: cyan },
        { label: 'S&P 500', data: series.bench, borderColor: muted,
          backgroundColor: 'transparent', borderWidth: 1.5, borderDash: [4, 3], fill: false,
          tension: 0.25, pointRadius: 0, pointHoverRadius: 4, pointHoverBackgroundColor: muted },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'top', align: 'end',
          labels: { color: textSec, font: { family: mono, size: 10 },
            boxWidth: 12, boxHeight: 2, padding: 16 } },
        tooltip: {
          backgroundColor: '#081422', borderColor: '#1e4060', borderWidth: 1,
          titleColor: textSec, bodyColor: '#f0f8ff', padding: 10,
          titleFont: { family: mono, size: 10 }, bodyFont: { family: mono, size: 11 },
          callbacks: { label: c => `${c.dataset.label}: ${c.parsed.y == null ? '—' : c.parsed.y.toFixed(1)}` },
        },
      },
      scales: {
        x: { grid: { display: false },
          ticks: { color: muted, font: { family: mono, size: 9 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 6 } },
        y: { grid: { color: 'rgba(30,64,96,0.25)' },
          ticks: { color: muted, font: { family: mono, size: 9 } } },
      },
    },
  });
}

// Diverging fill for a treemap tile by P&L% (green ↔ neutral grey ↔ red).
function _pnlTileColor(pct) {
  if (pct == null || Math.abs(pct) < 0.1) return { bg: 'rgba(90,122,148,0.12)', edge: 'rgba(90,122,148,0.4)' };
  const a = 0.16 + 0.5 * Math.min(Math.abs(pct) / 12, 1);
  return pct > 0
    ? { bg: `rgba(74,222,128,${a})`, edge: 'rgba(74,222,128,0.55)' }
    : { bg: `rgba(248,113,113,${a})`, edge: 'rgba(248,113,113,0.55)' };
}

// Squarified treemap (Bruls et al.) — lays each row along the shorter free side so
// tiles stay near-square. Returns [{data, x, y, w, h}] within a W×H box.
function _squarify(data, W, H) {
  const items = data.map(d => ({ d, area: Math.max(d.value, 0) }))
    .filter(o => o.area > 0).sort((a, b) => b.area - a.area);
  const totalV = items.reduce((s, o) => s + o.area, 0) || 1;
  const scale = (W * H) / totalV;
  items.forEach(o => (o.area *= scale));
  const area = { x: 0, y: 0, w: W, h: H };
  const out = [];
  const shortest = () => Math.min(area.w, area.h);
  const worst = (row, s) => {
    let sum = 0, mx = -Infinity, mn = Infinity;
    for (const o of row) { sum += o.area; mx = Math.max(mx, o.area); mn = Math.min(mn, o.area); }
    return Math.max((s * s * mx) / (sum * sum), (sum * sum) / (s * s * mn));
  };
  const layout = row => {
    const sum = row.reduce((a, o) => a + o.area, 0);
    const thick = sum / shortest();
    if (area.w >= area.h) {
      let cy = area.y;
      for (const o of row) { const ch = o.area / thick; out.push({ data: o.d, x: area.x, y: cy, w: thick, h: ch }); cy += ch; }
      area.x += thick; area.w -= thick;
    } else {
      let cx = area.x;
      for (const o of row) { const cw = o.area / thick; out.push({ data: o.d, x: cx, y: area.y, w: cw, h: thick }); cx += cw; }
      area.y += thick; area.h -= thick;
    }
  };
  let row = [];
  for (const o of items) {
    const s = shortest();
    if (row.length === 0 || worst([...row, o], s) <= worst(row, s)) row.push(o);
    else { layout(row); row = [o]; }
  }
  if (row.length) layout(row);
  return out;
}

window._astraTreemaps = window._astraTreemaps || new Map();

// Lay the treemap into the container's CURRENT box. Driven by a ResizeObserver so
// the tiles always fill the real width — a one-shot clientWidth read at render time
// can be stale (the panel is often still stretching to full width during page load).
function _layoutTreemap(container, clean) {
  if (!clean.length) { container.innerHTML = '<div class="treemap-empty">No open positions.</div>'; return; }
  // Height is a fixed CSS value (reliable even mid-load); width can be read stale while
  // the panel is still stretching. So squarify in the measured box, then emit x/width as
  // PERCENTAGES of that box — the row fills 100% of the container's real width no matter
  // what width we measured (no dependency on ResizeObserver/rAF timing). Height stays px.
  const W = container.clientWidth || 640;
  const H = container.clientHeight || 200;
  container._tmW = W;
  const rects = _squarify(clean, W, H);
  const estW = container.getBoundingClientRect().width || W;  // best-effort real px
  container.innerHTML = rects.map(r => {
    const d = r.data;
    const c = _pnlTileColor(d.pnlPct);
    const pxW = r.w / W * estW;          // rendered tile width in real px
    const pxH = r.h;                     // height space is already real px
    const pnlTxt = d.pnlPct == null ? '—' : fmtPct(d.pnlPct);
    // Scale the ticker so it always fits the tile (JetBrains Mono ≈ 0.6·fontSize wide),
    // capped by height. This is what keeps 4-letter tickers from ever clipping.
    const innerW = pxW - 12;
    let fTk = Math.floor(innerW / (d.ticker.length * 0.62));
    fTk = Math.max(8, Math.min(16, Math.min(fTk, Math.floor(pxH * 0.42))));
    const showTicker = pxW >= 22 && pxH >= 15;
    const showPnl = pxH >= 48 && pxW >= 46 && d.pnlPct != null;
    const fPnl = Math.max(8, Math.min(11, Math.round(fTk * 0.72)));
    const label = showTicker
      ? `<span class="treemap-tk" style="font-size:${fTk}px">${d.ticker}</span>` +
        (showPnl ? `<span class="treemap-pnl" style="font-size:${fPnl}px">${pnlTxt}</span>` : '')
      : '';
    const left = (r.x / W * 100).toFixed(4);
    const wPct = (r.w / W * 100).toFixed(4);
    return `<div class="treemap-tile" data-ticker="${d.ticker}"
      title="${d.ticker} · ${pnlTxt}${d.sub ? ' · ' + d.sub : ''}"
      style="left:${left}%;top:${r.y}px;width:calc(${wPct}% - 2px);height:${Math.max(r.h - 2, 0)}px;background:${c.bg};border-color:${c.edge}">
      ${label}
    </div>`;
  }).join('');
}

let _tmObserver = null;
function _ensureTreemapObserver() {
  if (_tmObserver || typeof ResizeObserver === 'undefined') return;
  _tmObserver = new ResizeObserver(entries => {
    for (const e of entries) {
      const items = window._astraTreemaps.get(e.target);
      // Only reflow on a real width change — guards against ResizeObserver feedback loops.
      if (items && Math.abs(e.target.clientWidth - (e.target._tmW || 0)) >= 2) {
        _layoutTreemap(e.target, items);
      }
    }
  });
}

function renderTreemap(container, items) {
  if (!container) return;
  const clean = (items || []).filter(i => i.value > 0);
  window._astraTreemaps.set(container, clean);
  _ensureTreemapObserver();
  if (_tmObserver) _tmObserver.observe(container); // fires once with the real box, then on resize
  _layoutTreemap(container, clean);                // immediate paint (re-corrected by the observer)
}

function renderAutotrader(sb, control, trades, snapshot, navRows, spyRows, marketData) {
  const section = document.getElementById("autotrader");
  if (!section) return;
  section.classList.remove('hidden');

  const halted = !!control.halted;
  const paused = !!control.paused;
  const statusLabel = halted ? 'HALTED' : (paused ? 'PAUSED' : 'ACTIVE');
  const statusColor = halted ? '#ff5c5c' : (paused ? '#e0a500' : '#38d977');

  const equity = snapshot?.total_equity;
  const drawdown = snapshot?.drawdown_pct;

  const open = trades.filter(t => t.is_open);
  const money = v => (v == null ? '—' : `$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`);

  const ddClass = (drawdown != null && drawdown < 0) ? 'negative' : '';

  // Holdings treemap from the latest snapshot's positions (ticker → shares, avg_cost);
  // current price from today's screen (marketData), cost-basis fallback when absent.
  const positions = snapshot?.positions || {};
  const holdings = Object.entries(positions).map(([ticker, p]) => {
    const shares = Number(p.shares) || 0;
    const avg = Number(p.avg_cost) || 0;
    const cur = marketData?.[ticker]?.current_price ?? null;
    const price = cur ?? avg;
    const value = shares * price;
    const pnlPct = (cur && avg) ? (cur - avg) / avg * 100 : null;
    return { ticker, value, pnlPct, sub: money(value) };
  }).filter(h => h.value > 0);

  const series = buildNavVsSpy(navRows, spyRows);
  const vsSpyHtml = series && series.vsSpy != null
    ? `<div class="paper-stat-value ${series.vsSpy >= 0 ? 'positive' : 'negative'}">${fmtPct(series.vsSpy)}</div>`
    : `<div class="paper-stat-value paper-stat-na">— <span class="paper-stat-note">&lt;2 runs</span></div>`;

  const orderRows = trades.map(t => {
    const sideCls = t.side === 'buy' ? 'positive' : 'negative';
    const size = t.dollar_amount != null ? money(t.dollar_amount) : (t.quantity != null ? `${t.quantity} sh` : '—');
    return `<tr>
      <td>${(t.run_date || '').slice(0, 10)}</td>
      <td class="pt-ticker">${t.ticker}</td>
      <td class="${sideCls}" style="font-weight:600">${(t.side || '').toUpperCase()}</td>
      <td>${size}</td>
      <td>${t.status || ''}${t.status === 'dry_run' ? ' <span style="color:var(--text-dim)">(sim)</span>' : ''}</td>
    </tr>`;
  }).join('');

  section.innerHTML = `
    <div class="section-label">
      <span class="section-icon">◆</span>
      <span>AUTOTRADER · AUTONOMOUS</span>
      <span class="section-badge">REAL-MONEY AGENTIC</span>
    </div>
    <div class="paper-card">
      <div class="at-header">
        <div class="at-status">
          <span class="at-dot" style="background:${statusColor};box-shadow:0 0 8px ${statusColor}"></span>
          <span style="color:${statusColor};font-weight:700;letter-spacing:1px">${statusLabel}</span>
          ${halted && control.halt_reason ? `<span class="at-halt">— ${control.halt_reason}</span>` : ''}
        </div>
        <button id="autotrader-toggle" class="paper-tab at-toggle" ${halted ? 'disabled title="Halted — reset in DB after review"' : ''}>
          ${paused ? '▶ RESUME' : '⏸ PAUSE'}
        </button>
      </div>
      <div class="perf-chart-wrap">
        ${series ? '<canvas id="at-perf-chart"></canvas>' : perfPlaceholder()}
      </div>
      <div class="paper-summary">
        <div class="paper-stat"><div class="paper-stat-label">EQUITY</div><div class="paper-stat-value">${money(equity)}</div></div>
        <div class="paper-stat-divider"></div>
        <div class="paper-stat"><div class="paper-stat-label">DRAWDOWN</div><div class="paper-stat-value ${ddClass}">${drawdown != null ? drawdown.toFixed(1) + '%' : '—'}</div></div>
        <div class="paper-stat-divider"></div>
        <div class="paper-stat"><div class="paper-stat-label">vs SPY</div>${vsSpyHtml}</div>
        <div class="paper-stat-divider"></div>
        <div class="paper-stat"><div class="paper-stat-label">OPEN POSITIONS</div><div class="paper-stat-value">${open.length}</div></div>
      </div>
      <div class="treemap-section">
        <div class="treemap-caption">HOLDINGS <span>· tile size = position weight · color = P&amp;L</span></div>
        <div class="treemap" id="at-treemap"></div>
      </div>
      <details class="activity-log">
        <summary>Activity log <span class="activity-count">${trades.length} orders</span></summary>
        ${trades.length ? `<div class="activity-table-wrap"><table class="paper-table">
          <thead><tr><th>DATE</th><th>TICKER</th><th>SIDE</th><th>SIZE</th><th>STATUS</th></tr></thead>
          <tbody>${orderRows}</tbody></table></div>`
          : '<div class="treemap-empty">No agentic orders yet.</div>'}
      </details>
    </div>
  `;

  if (series) renderEquityCurve(document.getElementById('at-perf-chart'), series, 'Autotrader');
  renderTreemap(document.getElementById('at-treemap'), holdings);

  const toggle = document.getElementById('autotrader-toggle');
  if (toggle && !halted) {
    toggle.addEventListener('click', async () => {
      toggle.disabled = true;
      const next = !paused;
      const { error } = await sb.from('agent_control')
        .update({ paused: next, updated_at: new Date().toISOString() }).eq('id', 1);
      if (error) {
        console.error('Autotrader pause toggle failed:', error.message);
        toggle.disabled = false;
        return;
      }
      logUserAction(next ? 'autotrader_pause' : 'autotrader_resume', null, { paused: next });
      renderAutotrader(sb, { ...control, paused: next }, trades, snapshot, navRows, spyRows, marketData);
    });
  }
}

// Returns { theme, status } for a ticker from cached convictions, for display in modal
function getTickerConvictionInfo(ticker) {
  if (!_convictions) return { theme: null, status: null, note: null };
  for (const [key, theme] of Object.entries(_convictions.themes || {})) {
    const lists = [
      { field: 'approved',   label: 'APPROVED'    },
      { field: 'preferred',  label: 'PREFERRED'   },
      { field: 'hold_only',  label: 'HOLD ONLY'   },
      { field: 'do_not_add', label: 'DO NOT ADD'  },
    ];
    for (const { field, label } of lists) {
      if ((theme[field] || []).includes(ticker)) {
        return {
          theme: key.replace(/_/g, ' ').toUpperCase(),
          status: label,
          note: theme.notes?.[ticker] || null,
        };
      }
    }
  }
  const h = (_convictions.individual_holdings || {})[ticker];
  if (h) return { theme: null, status: (h.status || 'hold').toUpperCase(), note: h.thesis || null };
  return { theme: null, status: null, note: null };
}

async function fetchRecentDecisionHistory(sb) {
  const since = new Date();
  since.setDate(since.getDate() - 45);
  const { data } = await sb
    .from('decisions')
    .select('ticker, action, run_date, price_at_decision')
    .gte('run_date', since.toISOString())
    .order('run_date', { ascending: false })
    .limit(300);
  // Deduplicate: keep only the most recent entry per ticker per local calendar day
  // (UTC slice won't work — runs near midnight PT land on different UTC dates)
  const byTicker = {};
  const seen = new Set();
  (data || []).forEach(d => {
    const localDay = new Date(d.run_date).toLocaleDateString('en-CA'); // YYYY-MM-DD local tz
    const key = `${d.ticker}:${localDay}`;
    if (seen.has(key)) return;
    seen.add(key);
    if (!byTicker[d.ticker]) byTicker[d.ticker] = [];
    byTicker[d.ticker].push(d);
  });
  return byTicker;
}

// ── Paper portfolio ──────────────────────────────────────────

function renderPaperPortfolio(paperTrades, marketData, navRows, spyRows) {
  const section = document.getElementById('paper-portfolio');
  if (!paperTrades.length) { section.classList.add('hidden'); return; }
  section.classList.remove('hidden');

  let totalCost = 0, totalValue = 0;
  const holdMap = {};   // aggregate lots by ticker for the treemap
  const rows = paperTrades.map(pt => {
    const mdata = marketData[pt.ticker] || {};
    const cur   = mdata.current_price ?? pt.price_at_signal;
    const pnlD  = (cur - pt.price_at_signal) * pt.virtual_shares;
    const pnlPct = (cur - pt.price_at_signal) / pt.price_at_signal * 100;
    totalCost  += pt.virtual_cost;
    totalValue += cur * pt.virtual_shares;

    const h = holdMap[pt.ticker] || (holdMap[pt.ticker] = { ticker: pt.ticker, value: 0, cost: 0, pnlD: 0 });
    h.value += cur * pt.virtual_shares;
    h.cost  += pt.virtual_cost;
    h.pnlD  += pnlD;

    // Bar: centred at 0, ±15% = full width
    const clampedPct  = Math.max(-15, Math.min(15, pnlPct));
    const barWidth    = Math.abs(clampedPct) / 15 * 50; // % of half-width
    const barPos      = pnlPct >= 0 ? 'right' : 'left';
    const barColor    = pnlPct >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
    const sinceDate   = new Date(pt.run_date).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });

    return `
      <div class="paper-row">
        <div class="paper-row-ticker">
          <span class="paper-ticker">${pt.ticker}</span>
          <span class="paper-since">since ${sinceDate}</span>
        </div>
        <div class="paper-row-prices">
          <span class="paper-entry">${fmtPrice(pt.price_at_signal)}</span>
          <span class="paper-arrow">→</span>
          <span class="paper-cur">${fmtPrice(cur)}</span>
        </div>
        <div class="paper-bar-wrap">
          <div class="paper-bar-track">
            <div class="paper-bar-center"></div>
            <div class="paper-bar-fill" style="width:${barWidth}%; ${barPos}:50%; background:${barColor};"></div>
          </div>
        </div>
        <div class="paper-pnl ${pnlD >= 0 ? 'positive' : 'negative'}">
          ${pnlD >= 0 ? '+' : ''}${fmtBigNum(pnlD)} <span class="paper-pnl-pct">${fmtPct(pnlPct)}</span>
        </div>
      </div>`;
  });

  const totalPnlD   = totalValue - totalCost;
  const totalPnlPct = totalCost > 0 ? (totalValue - totalCost) / totalCost * 100 : 0;

  const holdings = Object.values(holdMap).map(h => ({
    ticker: h.ticker, value: h.value,
    pnlPct: h.cost > 0 ? h.pnlD / h.cost * 100 : null,
    sub: fmtBigNum(h.value),
  }));

  const series = buildNavVsSpy(navRows, spyRows);
  const vsSpyHtml = series && series.vsSpy != null
    ? `<div class="paper-stat-value ${series.vsSpy >= 0 ? 'positive' : 'negative'}" title="Since-inception return vs SPY, both rebased to 100">${fmtPct(series.vsSpy)}</div>`
    : `<div class="paper-stat-value paper-stat-na" title="Needs ≥2 daily runs for a benchmark comparison">— <span class="paper-stat-note">&lt;2 runs</span></div>`;

  const closedTrades = window._astraClosedPaperTrades || [];
  const closedPnlD = closedTrades.reduce((sum, pt) => {
    if (!pt.close_price || !pt.price_at_signal) return sum;
    return sum + (pt.close_price - pt.price_at_signal) * (pt.virtual_shares || 0);
  }, 0);
  const CLOSE_REASON_LABEL = {
    signal_inactive: 'Signal ended',
    profit_take:     'Profit taken',
    blocked:         'Blocked',
  };

  const closedRowsHtml = closedTrades.length === 0
    ? '<div style="padding:24px 0;font-family:var(--font-mono);font-size:11px;color:var(--text-dim);text-align:center;letter-spacing:1px;">No closed paper trades in the last 90 days.</div>'
    : closedTrades.map(pt => {
        const pnlD   = pt.close_price ? (pt.close_price - pt.price_at_signal) * pt.virtual_shares : null;
        const pnlPct = pt.close_price ? (pt.close_price - pt.price_at_signal) / pt.price_at_signal * 100 : null;
        const cls    = pnlD != null ? (pnlD >= 0 ? 'positive' : 'negative') : '';
        const openDate  = new Date(pt.run_date).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
        const closeDate = pt.closed_at ? new Date(pt.closed_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : '—';
        const reasonLabel = CLOSE_REASON_LABEL[pt.close_reason] || pt.close_reason || '—';
        return `<div class="paper-row paper-closed-row">
          <div class="paper-row-ticker">
            <span class="paper-ticker">${pt.ticker}</span>
            <span class="paper-since">${openDate} → ${closeDate}</span>
          </div>
          <div class="paper-row-prices">
            <span class="paper-entry">${fmtPrice(pt.price_at_signal)}</span>
            <span class="paper-arrow">→</span>
            <span class="paper-cur">${pt.close_price ? fmtPrice(pt.close_price) : '—'}</span>
          </div>
          <div class="paper-close-reason">${reasonLabel}</div>
          <div class="paper-pnl ${cls}">
            ${pnlD != null ? (pnlD >= 0 ? '+' : '') + fmtBigNum(pnlD) + ' <span class="paper-pnl-pct">' + fmtPct(pnlPct) + '</span>' : '—'}
          </div>
        </div>`;
      }).join('');

  section.innerHTML = `
    <div class="section-label">
      <span class="section-icon">◈</span>
      <span>PAPER PORTFOLIO</span>
      <span class="section-badge">ASTRA SIMULATION</span>
    </div>
    <div class="paper-card">
      <div class="perf-chart-wrap">
        ${series ? '<canvas id="paper-perf-chart"></canvas>' : perfPlaceholder()}
      </div>
      <div class="paper-summary">
        <div class="paper-stat">
          <div class="paper-stat-label">OPEN P&amp;L</div>
          <div class="paper-stat-value ${totalPnlD >= 0 ? 'positive' : 'negative'}">
            ${totalPnlD >= 0 ? '+' : ''}${fmtBigNum(totalPnlD)}
            <span class="paper-stat-pct">${fmtPct(totalPnlPct)}</span>
          </div>
        </div>
        <div class="paper-stat-divider"></div>
        <div class="paper-stat">
          <div class="paper-stat-label">CLOSED P&amp;L</div>
          <div class="paper-stat-value ${closedPnlD >= 0 ? 'positive' : 'negative'}" title="Realised P&amp;L on closed paper positions (last 90 days)">
            ${closedTrades.length ? (closedPnlD >= 0 ? '+' : '') + fmtBigNum(closedPnlD) : '<span class="paper-stat-na">—</span>'}
          </div>
        </div>
        <div class="paper-stat-divider"></div>
        <div class="paper-stat">
          <div class="paper-stat-label">OPEN / CLOSED</div>
          <div class="paper-stat-value">${paperTrades.length} <span style="color:var(--text-dim);font-size:13px;">/</span> ${closedTrades.length}</div>
        </div>
        <div class="paper-stat-divider"></div>
        <div class="paper-stat">
          <div class="paper-stat-label">vs SPY</div>
          ${vsSpyHtml}
        </div>
      </div>
      <div class="treemap-section">
        <div class="treemap-caption">OPEN POSITIONS <span>· tile size = position weight · color = P&amp;L</span></div>
        <div class="treemap" id="paper-treemap"></div>
      </div>
      <details class="activity-log">
        <summary>Position detail <span class="activity-count">${paperTrades.length} open / ${closedTrades.length} closed</span></summary>
        <div class="paper-tab-bar">
          <button class="paper-tab active" id="paper-tab-open">OPEN <span class="paper-tab-count">${paperTrades.length}</span></button>
          <button class="paper-tab" id="paper-tab-closed">CLOSED <span class="paper-tab-count">${closedTrades.length}</span></button>
        </div>
        <div id="paper-open-panel" class="paper-rows">
          <div class="paper-rows-header"><span>POSITION</span><span>ENTRY → NOW</span><span></span><span>P&amp;L</span></div>
          ${rows.join('')}
        </div>
        <div id="paper-closed-panel" class="paper-rows hidden">
          <div class="paper-rows-header paper-closed-header"><span>POSITION</span><span>ENTRY → EXIT</span><span>REASON</span><span>P&amp;L</span></div>
          ${closedRowsHtml}
        </div>
      </details>
    </div>`;

  if (series) renderEquityCurve(document.getElementById('paper-perf-chart'), series, 'Paper NAV');
  renderTreemap(document.getElementById('paper-treemap'), holdings);

  // Tab switching
  document.getElementById('paper-tab-open')?.addEventListener('click', () => {
    document.getElementById('paper-tab-open').classList.add('active');
    document.getElementById('paper-tab-closed').classList.remove('active');
    document.getElementById('paper-open-panel').classList.remove('hidden');
    document.getElementById('paper-closed-panel').classList.add('hidden');
  });
  document.getElementById('paper-tab-closed')?.addEventListener('click', () => {
    document.getElementById('paper-tab-closed').classList.add('active');
    document.getElementById('paper-tab-open').classList.remove('active');
    document.getElementById('paper-closed-panel').classList.remove('hidden');
    document.getElementById('paper-open-panel').classList.add('hidden');
  });
}

// ── Position card ────────────────────────────────────────────

function buildCard(ticker, mdata, signal, decision, paperTrade) {
  const card = document.createElement('div');
  const action = signal ? signal.action : 'hold';

  card.className = `pos-card signal-${action}`;
  card.dataset.ticker = ticker;

  const price   = mdata?.current_price;
  const avgCost = isAuthenticated ? decision?.avg_cost : null;

  let pnlHtml = '';
  if (avgCost && price) {
    const pnl = ((price - avgCost) / avgCost) * 100;
    const cls = pnl >= 0 ? 'positive' : 'negative';
    pnlHtml = `<span class="card-pnl ${cls}">${fmtPct(pnl)}</span>`;
  }

  const shortName = mdata?.short_name
    ? (mdata.short_name.length > 24 ? mdata.short_name.slice(0, 24) + '…' : mdata.short_name)
    : '';

  const badgeLabel = action.toUpperCase();

  // Paper trade P&L badge (auth-gated, shows if ASTRA has an open virtual position)
  let paperBadgeHtml = '';
  if (paperTrade && mdata?.current_price) {
    const pnlPct = (mdata.current_price - paperTrade.price_at_signal) / paperTrade.price_at_signal * 100;
    const cls = pnlPct >= 0 ? 'positive' : 'negative';
    paperBadgeHtml = `<span class="card-paper-badge ${cls}" title="ASTRA paper trade: ${fmtPct(pnlPct)} since ${new Date(paperTrade.run_date).toLocaleDateString('en-US',{month:'short',day:'numeric'})}">PAPER ${fmtPct(pnlPct)}</span>`;
  }

  // For actionable signals: show top 2 reasons why ASTRA flagged this
  let bodyHtml = '';
  if (['buy', 'watch', 'sell'].includes(action) && signal?.reasons?.length) {
    const topReasons = signal.reasons.slice(0, 2);
    const icons = { buy: '▲', watch: '◈', sell: '↓' };
    const icon = icons[action] || '•';
    bodyHtml = `<div class="card-reasons">
      ${topReasons.map(r => `
        <div class="card-reason">
          <span class="card-reason-icon good">${icon}</span>
          <span>${cleanReason(r)}</span>
        </div>`).join('')}
      ${signal.risk_flags?.length ? `<div class="card-reason">
        <span class="card-reason-icon warn">⚠</span>
        <span>${signal.risk_flags[0]}</span>
      </div>` : ''}
    </div>`;
  } else if (action === 'blocked' && signal?.reasons?.length) {
    const raw = cleanReason(signal.reasons[0]);
    const short = raw.length > 80 ? raw.slice(0, 80) + '…' : raw;
    bodyHtml = `<div class="card-reasons">
      <div class="card-reason">
        <span class="card-reason-icon block">✕</span>
        <span>${short}</span>
      </div>
    </div>`;
  } else {
    // Hold: just show RSI as a single compact stat
    const rsi = mdata?.rsi_14;
    const pctBelow = mdata?.pct_below_52w_high;
    const parts = [];
    if (rsi != null) parts.push(`RSI ${fmt(rsi, 0)}`);
    if (pctBelow != null) parts.push(`↓${fmt(pctBelow, 0)}% peak`);
    if (parts.length) bodyHtml = `<div class="card-hold-metric">${parts.join(' · ')}</div>`;
  }

  card.innerHTML = `
    <div class="card-top-row">
      <span class="card-ticker">${ticker}</span>
      <span class="signal-badge badge-${action}">${badgeLabel}</span>
    </div>
    ${paperBadgeHtml}
    <div class="card-name">${shortName}</div>
    <div class="card-price-row">
      <span class="card-price">${fmtPrice(price)}</span>
      ${pnlHtml}
    </div>
    ${bodyHtml}
  `;

  card.addEventListener('click', () => openModal(ticker, mdata, signal, decision, paperTrade));
  return card;
}

// ── Swim lanes ───────────────────────────────────────────────

function renderLanes(marketData, signalsByTicker, decisionsByTicker, paperTradesByTicker) {
  const container = document.getElementById('lanes-container');
  container.innerHTML = '';

  // Build set of all tickers we have data for
  const allTickers = Object.keys(marketData);

  // Assign tickers not in any explicit theme to 'other'
  const otherTickers = allTickers.filter(t => {
    for (const [key, cfg] of Object.entries(THEME_MAP)) {
      if (key === 'other') continue;
      if (cfg.tickers.includes(t)) return false;
    }
    return true;
  });
  THEME_MAP.other.tickers = otherTickers;

  for (const [themeKey, cfg] of Object.entries(THEME_MAP)) {
    const tickersInLane = cfg.tickers.filter(t => allTickers.includes(t));
    if (tickersInLane.length === 0) continue;

    const lane = document.createElement('div');
    lane.className = 'lane';
    lane.dataset.theme = themeKey;

    const convBadge = cfg.conviction
      ? `<span class="lane-conviction conviction-${cfg.conviction}" title="ASTRA's conviction in this theme's investment thesis">CONVICTION · ${cfg.conviction.replace('_', ' ').toUpperCase()}</span>`
      : '';

    lane.innerHTML = `
      <div class="lane-header">
        <span class="lane-theme-name">${cfg.label}</span>
        ${convBadge}
        <span class="lane-count">${tickersInLane.length} positions</span>
      </div>
      <div class="cards-grid"></div>
    `;

    const grid = lane.querySelector('.cards-grid');

    // Sort: buy first, then watch, review, hold, blocked
    const ORDER = { buy: 0, sell: 1, watch: 2, hold: 3, blocked: 4 };
    tickersInLane.sort((a, b) => {
      const aAction = signalsByTicker[a]?.action ?? 'hold';
      const bAction = signalsByTicker[b]?.action ?? 'hold';
      return (ORDER[aAction] ?? 3) - (ORDER[bAction] ?? 3);
    });

    for (const ticker of tickersInLane) {
      const card = buildCard(
        ticker,
        marketData[ticker],
        signalsByTicker[ticker] || null,
        decisionsByTicker[ticker] || null,
        paperTradesByTicker?.[ticker] || null,
      );
      grid.appendChild(card);
    }

    container.appendChild(lane);
  }
}

// ── Modal ────────────────────────────────────────────────────

let modalBarChart = null;

function openModal(ticker, mdata, signal, decision, paperTrade) {
  const overlay = document.getElementById('modal-overlay');
  const content = document.getElementById('modal-content');
  overlay.classList.remove('hidden');
  document.body.style.overflow = 'hidden';
  if (modalBarChart) { modalBarChart.destroy(); modalBarChart = null; }

  const action    = signal?.action ?? 'hold';
  const price     = mdata?.current_price;
  const avgCost   = isAuthenticated ? decision?.avg_cost : null;
  const rsi       = mdata?.rsi_14;
  const pctBelow  = mdata?.pct_below_52w_high;
  const revGrowth = mdata?.revenue_growth_yoy != null ? mdata.revenue_growth_yoy * 100 : null;
  const grossMgn  = mdata?.gross_margins != null ? mdata.gross_margins * 100 : null;

  let pnlVal = null;
  if (avgCost && price) pnlVal = ((price - avgCost) / avgCost) * 100;

  const reasons = signal?.reasons ?? [];
  const risks   = signal?.risk_flags ?? [];

  // ASTRA verdict row
  const convInfo = getTickerConvictionInfo(ticker);
  const convLabel = signal?.conviction_match
    ? (convInfo.theme && convInfo.status
        ? `✓ ${convInfo.theme} · ${convInfo.status}`
        : '✓ MATCH')
    : signal ? '✗ NO MATCH' : '—';

  const techSubParts = [];
  if (rsi != null) techSubParts.push(`RSI ${rsi.toFixed(1)} ${rsi < 40 ? '< 40 ✓' : `(need <40)`}`);
  if (pctBelow != null) techSubParts.push(`${pctBelow.toFixed(1)}% below 52w ${pctBelow >= 15 ? '✓' : `(need 15%)`}`);
  const techSub = techSubParts.join(' · ') || null;

  const qualSub = (revGrowth != null || grossMgn != null)
    ? [revGrowth != null ? `Rev ${revGrowth >= 0 ? '+' : ''}${revGrowth.toFixed(0)}%` : null,
       grossMgn  != null ? `Mgn ${grossMgn.toFixed(0)}%`  : null].filter(Boolean).join(' · ')
    : null;

  const verdictItems = [
    {
      label: 'Conviction', cls: signal?.conviction_match ? 'pass' : signal ? 'fail' : 'na',
      value: convLabel, sub: null,
      tip: 'Does this ticker appear in your approved conviction themes? E.g. space, core tech, EV.',
    },
    {
      label: 'Quality', cls: signal?.quality_pass ? 'pass' : signal ? 'fail' : 'na',
      value: signal?.quality_pass ? '✓ PASS' : signal ? '✗ FAIL' : '—', sub: qualSub,
      tip: 'Passes ASTRA quality filter: revenue growth >10% YoY, gross margin >30%, manageable debt.',
    },
    {
      label: 'Technical', cls: signal?.technical_pass ? 'pass' : signal ? 'warn' : 'na',
      value: signal?.technical_pass ? '✓ ENTRY' : signal ? '✗ NOT YET' : '—', sub: techSub,
      tip: 'Technical entry signal: price >15% below 52-week high AND RSI below 40 (oversold dip).',
    },
    {
      label: 'Hard Rules', cls: signal?.hard_rule_block ? 'fail' : signal ? 'pass' : 'na',
      value: signal?.hard_rule_block ? '✗ BLOCKED' : signal ? '✓ CLEAR' : '—', sub: null,
      tip: 'Hard constraint check: not TSLA, not averaging down past 3x on positions >35% below cost, not a "hold only" ticker.',
    },
  ];

  const verdictHtml = verdictItems.map(v => `
    <div class="verdict-item" title="${v.tip}">
      <div class="verdict-label">${v.label}</div>
      <div class="verdict-value ${v.cls}">${v.value}</div>
      ${v.sub ? `<div class="verdict-sub">${v.sub}</div>` : ''}
    </div>`).join('');

  // Signal reasons — split long single-string reasons into sentence bullets
  function reasonToBullets(r) {
    const cleaned = cleanReason(r);
    const sentences = cleaned.split(/\.\s+/).map(s => s.trim()).filter(Boolean);
    if (sentences.length <= 1) return `<div class="reason-item"><span class="icon">✓</span>${cleaned}</div>`;
    return sentences.map((s, i) =>
      `<div class="reason-item"><span class="icon">·</span>${s}${i < sentences.length - 1 ? '.' : ''}</div>`
    ).join('');
  }
  const reasonsHtml = reasons.length
    ? reasons.map(reasonToBullets).join('')
    : '<div class="no-data">No signal reasons — holding position</div>';

  const risksHtml = risks.length
    ? risks.map(r => `<div class="risk-item"><span class="icon">⚠</span>${cleanReason(r)}</div>`).join('')
    : '<div class="no-data">No risk flags identified</div>';

  const suggestedHtml = (isAuthenticated && signal?.suggested_position_pct != null)
    ? `<div class="suggested-size">Suggested size: ${(signal.suggested_position_pct * 100).toFixed(0)}% of portfolio</div>`
    : '';

  // Supplemental fundamentals — [label, value, tooltip]
  const fundItems = [
    ['Market Cap',    fmtBigNum(mdata?.market_cap),
      'Total market value of all shares outstanding. Indicates company size and risk profile.'],
    ['Fwd P/E',       fmt(mdata?.forward_pe, 1),
      'Price ÷ next year\'s estimated earnings. Lower = cheaper relative to expected growth; high P/E means growth is already priced in.'],
    ['D/E Ratio',     fmt(mdata?.debt_to_equity, 1),
      'Total debt ÷ shareholder equity. High values = more leveraged balance sheet and higher bankruptcy risk in downturns.'],
    ['Current Ratio', fmt(mdata?.current_ratio, 2),
      'Current assets ÷ current liabilities. Above 1.0 = can cover short-term obligations. Below 1.0 = potential liquidity risk.'],
    ['Free Cash Flow',fmtBigNum(mdata?.free_cashflow),
      'Cash generated after capital expenditures. Positive = self-funding growth; negative = burning cash and may need to raise.'],
    ['MA 50',         fmtPrice(mdata?.ma_50),
      '50-day moving average price. Short-term trend indicator. Price above MA50 = recent upward momentum.'],
    ['MA 200',        fmtPrice(mdata?.ma_200),
      '200-day moving average price. Long-term trend indicator. Price above MA200 = confirmed long-term uptrend.'],
    ['vs MA50',       mdata?.price_vs_ma50_pct != null ? fmtPct(mdata.price_vs_ma50_pct) : '—',
      'Current price relative to 50-day average. Negative = trading below recent trend, which may signal an oversold dip entry.'],
  ];

  const fundHtml = fundItems.map(([l, v, tip]) => `
    <div class="modal-metric-sm" title="${tip}">
      <div class="modal-metric-sm-label">${l}</div>
      <div class="modal-metric-sm-value">${v}</div>
    </div>`).join('');

  // P&L display
  const pnlDisplay = pnlVal != null
    ? `<span style="font-family:var(--font-mono); font-size:14px; font-weight:600; color:${pnlVal >= 0 ? 'var(--accent-green)' : 'var(--accent-red)'}; margin-left:12px;">${fmtPct(pnlVal)}</span>`
    : '';

  const avgCostDisplay = avgCost
    ? `<span style="font-family:var(--font-mono); font-size:12px; color:var(--text-muted); margin-left:8px;">avg ${fmtPrice(avgCost)}</span>`
    : '';

  content.innerHTML = `
    <div class="modal-header">
      <div>
        <div class="modal-ticker">${ticker}${pnlDisplay}</div>
        <div class="modal-name">${mdata?.short_name ?? ticker}${avgCostDisplay}</div>
        <div class="modal-sector">${[mdata?.sector, mdata?.industry].filter(Boolean).join(' · ')}</div>
      </div>
      <span class="signal-badge badge-${action}" style="font-size:12px; padding:5px 14px; align-self:flex-start;">${action.toUpperCase()}</span>
    </div>

    <!-- CONVICTION NOTE (auth-gated) -->
    ${isAuthenticated && convInfo.note ? `<div class="modal-conv-note"><span class="modal-conv-note-icon">◆</span>${convInfo.note}</div>` : ''}

    <!-- ASTRA VERDICT -->
    <div class="modal-section-title">ASTRA VERDICT</div>
    <div class="modal-verdict">${verdictHtml}</div>

    <!-- WHY ASTRA FLAGGED THIS -->
    <div class="modal-analysis">
      <div class="modal-block">
        <div class="modal-block-title">Why ASTRA flagged this</div>
        ${reasonsHtml}
        ${suggestedHtml}
      </div>
      <div class="modal-block">
        <div class="modal-block-title">Risk flags</div>
        ${risksHtml}
      </div>
    </div>

    <hr class="modal-divider">

    <!-- TECHNICAL CONTEXT -->
    <div class="modal-section-title">Technical Context</div>
    <div class="modal-chart-row">
      <div class="modal-chart-card">
        <div class="modal-chart-title">RSI — Relative Strength Index (14-day)</div>
        <div style="font-family:var(--font-mono); font-size:11px; color:var(--text-muted); letter-spacing:1px; margin-bottom:8px;">momentum gauge · &lt;40 oversold = potential buy · &gt;60 overbought = elevated risk</div>
        <div class="rsi-gauge-wrap">
          <div class="rsi-value-display" style="color:${rsiLabelColor(rsi)}">${rsi != null ? fmt(rsi, 1) : '—'}</div>
          <div style="font-family:var(--font-mono); font-size:10px; letter-spacing:2px; color:${rsiLabelColor(rsi)}; margin-bottom:12px;">${rsiLabel(rsi)}</div>
          <div class="rsi-gauge">
            <div class="rsi-needle" style="left:${rsi != null ? Math.min(rsi, 99) : 50}%"></div>
          </div>
          <div class="rsi-zones">
            <span>0 OVERSOLD</span><span>40</span><span>60</span><span>OVERBOUGHT 100</span>
          </div>
        </div>
      </div>
      <div class="modal-chart-card">
        <div class="modal-chart-title">Screening Metrics vs Thresholds</div>
        <canvas id="modal-bar-chart" height="130"></canvas>
      </div>
    </div>

    <hr class="modal-divider">

    <!-- SUPPLEMENTAL FUNDAMENTALS -->
    <div class="modal-section-title">Fundamentals (supplemental — also in Robinhood)</div>
    <div class="modal-metrics-grid">${fundHtml}</div>

    ${paperTrade ? `
    <hr class="modal-divider">
    <div class="modal-section-title">Paper Trade</div>
    <div class="modal-paper-trade">
      <div class="modal-paper-row">
        <span class="modal-paper-label">Entry price</span>
        <span class="modal-paper-val">${fmtPrice(paperTrade.price_at_signal)}</span>
      </div>
      <div class="modal-paper-row">
        <span class="modal-paper-label">Virtual shares</span>
        <span class="modal-paper-val">${paperTrade.virtual_shares.toFixed(4)}</span>
      </div>
      <div class="modal-paper-row">
        <span class="modal-paper-label">Virtual cost</span>
        <span class="modal-paper-val">${fmtPrice(paperTrade.virtual_cost)}</span>
      </div>
      ${mdata?.current_price ? (() => {
        const pnlD   = (mdata.current_price - paperTrade.price_at_signal) * paperTrade.virtual_shares;
        const pnlPct = (mdata.current_price - paperTrade.price_at_signal) / paperTrade.price_at_signal * 100;
        return `<div class="modal-paper-row">
          <span class="modal-paper-label">Current P&L</span>
          <span class="modal-paper-val ${pnlD >= 0 ? 'positive' : 'negative'}">${pnlD >= 0 ? '+' : ''}${fmtBigNum(pnlD)} (${fmtPct(pnlPct)})</span>
        </div>`;
      })() : ''}
      <div class="modal-paper-row">
        <span class="modal-paper-label">Opened</span>
        <span class="modal-paper-val">${new Date(paperTrade.run_date).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}</span>
      </div>
    </div>` : ''}

    <hr class="modal-divider">
    <div class="modal-section-title">Signal History <span style="color:var(--text-dim);font-size:9px;margin-left:8px;">last 45 days</span></div>
    <div class="modal-signal-history" id="modal-signal-history-${ticker}">
      <div class="signal-history-empty">Loading…</div>
    </div>
  `;

  // Signal history timeline
  requestAnimationFrame(() => {
    const histEl = document.getElementById(`modal-signal-history-${ticker}`);
    if (!histEl) return;
    const hist = (window._astraSignalHistory || {})[ticker] || [];
    if (!hist.length) {
      histEl.innerHTML = '<div class="signal-history-empty">No history yet — data builds up over daily runs.</div>';
      return;
    }
    const ACTION_DOT = { buy: '▲', sell: '↓', watch: '◈', blocked: '✕', hold: '·' };
    const ACTION_CLS = { buy: 'sh-buy', sell: 'sh-sell', watch: 'sh-watch', blocked: 'sh-blocked', hold: 'sh-hold' };
    histEl.innerHTML = hist.slice(0, 12).map((d, i) => `
      <div class="sh-row">
        <span class="sh-date">${new Date(d.run_date).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}</span>
        <span class="sh-dot ${ACTION_CLS[d.action] || 'sh-hold'}">${ACTION_DOT[d.action] || '·'}</span>
        <span class="sh-action ${ACTION_CLS[d.action] || 'sh-hold'}">${(d.action || 'hold').toUpperCase()}</span>
        <span class="sh-price">${fmtPrice(d.price_at_decision)}</span>
        ${i === 0 ? '<span class="sh-today">← today</span>' : ''}
      </div>`).join('');
  });

  // Bar chart
  requestAnimationFrame(() => {
    const barCtx = document.getElementById('modal-bar-chart');
    if (!barCtx) return;

    const items = [
      { label: 'RSI',        val: rsi ?? 0,                                    threshold: 40,  reverse: true  },
      { label: '% Bel 52W',  val: pctBelow ?? 0,                               threshold: 15,  reverse: false },
      { label: 'Rev Growth', val: Math.max(0, Math.min(100, revGrowth ?? 0)),  threshold: 10,  reverse: false },
      { label: 'Gr Margin',  val: Math.max(0, Math.min(100, grossMgn ?? 0)),   threshold: 30,  reverse: false },
    ];

    const colors = items.map(({ val, threshold, reverse }) => {
      const passes = reverse ? val < threshold : val > threshold;
      return passes ? 'rgba(57,211,83,0.75)' : 'rgba(255,107,53,0.6)';
    });

    modalBarChart = new Chart(barCtx, {
      type: 'bar',
      data: {
        labels: items.map(i => i.label),
        datasets: [
          {
            label: 'Value',
            data: items.map(i => i.val),
            backgroundColor: colors,
            borderRadius: 4,
            borderSkipped: false,
          },
          {
            label: 'Threshold',
            data: items.map(i => i.threshold),
            backgroundColor: 'rgba(255,255,255,0.08)',
            borderRadius: 4,
            borderSkipped: false,
          },
        ],
      },
      options: {
        indexAxis: 'y',
        scales: {
          x: { min: 0, max: 100, grid: { color: 'rgba(30,64,96,0.5)' }, ticks: { color: '#5a7a94', font: { family: 'JetBrains Mono', size: 9 }, maxTicksLimit: 5 }, border: { color: 'rgba(30,64,96,0.5)' } },
          y: { grid: { display: false }, ticks: { color: '#a8bdd0', font: { family: 'JetBrains Mono', size: 10 } }, border: { display: false } },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: { label: ctx => ` ${ctx.dataset.label}: ${ctx.parsed.x.toFixed(1)}` },
            backgroundColor: 'rgba(8,20,34,0.97)', borderColor: 'rgba(46,96,144,1)', borderWidth: 1,
            titleColor: '#a8bdd0', bodyColor: '#f0f8ff',
            titleFont: { family: 'JetBrains Mono', size: 10 }, bodyFont: { family: 'JetBrains Mono', size: 12 },
          },
        },
        animation: { duration: 500 },
      },
    });
  });
}

function closeModal() {
  document.getElementById('modal-overlay').classList.add('hidden');
  document.body.style.overflow = '';
  if (modalBarChart) { modalBarChart.destroy(); modalBarChart = null; }
}

// ── Convictions Drawer ───────────────────────────────────────

let _sb = null;
let _convs = null;
let _convRowId = null;
let _saveTimer = null;

const CONVICTION_LEVELS = ['low', 'medium', 'high', 'very_high'];
const CONVICTION_LABELS  = { low: 'LOW', medium: 'MED', high: 'HIGH', very_high: 'V.HIGH' };
const INTENT_OPTIONS = [
  { val: 'thesis_hold',   icon: '🎯', line1: 'LONG',    line2: 'TERM'  },
  { val: 'opportunistic', icon: '⚡', line1: 'OPP.',    line2: ''      },
  { val: 'written_off',   icon: '✕',  line1: 'WRITTEN', line2: 'OFF'   },
];

function setSaveStatus(state, msg) {
  const el = document.getElementById('conv-save-status');
  if (!el) return;
  el.className = 'conv-save-status ' + (state || '');
  el.textContent = msg || '';
}

function scheduleAutosave() {
  setSaveStatus('saving', '● Saving…');
  clearTimeout(_saveTimer);
  _saveTimer = setTimeout(doSaveConvictions, 900);
}

async function doSaveConvictions() {
  if (!_sb || !_convs || !_convRowId) return;
  try {
    const { error } = await _sb.from('convictions')
      .update({ content: _convs, updated_at: new Date().toISOString(), updated_by: 'dashboard' })
      .eq('id', _convRowId);
    if (error) throw error;
    // Historize the edit like the Python save path (conviction_history) + log the event.
    _sb.from('conviction_history')
      .insert({ content: _convs, saved_at: new Date().toISOString() })
      .then(({ error: histErr }) => {
        if (histErr) console.error('conviction_history append failed:', histErr.message);
      });
    logUserAction('conviction_edit', null, {});
    setSaveStatus('saved', '● Saved just now');
    setTimeout(() => setSaveStatus('', ''), 3000);
    // Rebuild swim lanes from updated convictions
    THEME_MAP = buildThemeMapFromConvictions(_convs);
    _convictions = JSON.parse(JSON.stringify(_convs));
    renderLanes(_lastMarketData, _lastSignalsByTicker, _lastDecisionsByTicker, _lastPaperByTicker);
  } catch (err) {
    setSaveStatus('error', '● Save failed');
    console.error('Convictions save error:', err.message);
    if (err.code === '42501' || (err.message || '').includes('policy')) {
      console.warn(
        'Add this in Supabase SQL Editor:\n' +
        "CREATE POLICY \"owner update convictions\" ON convictions\n" +
        "  FOR UPDATE TO authenticated USING (auth.email() = 'abhikirk@icloud.com');\n" +
        "GRANT UPDATE ON convictions TO authenticated;"
      );
    }
  }
}

function openConvictionsDrawer() {
  document.getElementById('conv-backdrop').classList.remove('hidden');
  document.getElementById('conv-drawer').classList.remove('hidden');
  document.body.style.overflow = 'hidden';
  loadAndRenderConvictions();
}

function closeConvictionsDrawer() {
  document.getElementById('conv-backdrop').classList.add('hidden');
  document.getElementById('conv-drawer').classList.add('hidden');
  document.body.style.overflow = '';
}

async function loadAndRenderConvictions() {
  const body = document.getElementById('conv-body');
  body.innerHTML = '<div class="conv-loading">Loading convictions…</div>';
  try {
    const { data, error } = await _sb
      .from('convictions')
      .select('id, content')
      .order('id', { ascending: false })
      .limit(1)
      .single();
    if (error) throw error;
    _convs = JSON.parse(JSON.stringify(data.content));
    _convRowId = data.id;
    renderConvDrawer();
  } catch (err) {
    body.innerHTML = `<div class="conv-loading" style="color:var(--accent-red)">Failed to load: ${err.message}</div>`;
  }
}

function renderConvDrawer() {
  const body = document.getElementById('conv-body');
  const themes   = _convs.themes || {};
  const holdings = _convs.individual_holdings || {};
  const tickerMeta = _convs.ticker_metadata || {};
  const allMetaTickers = Object.keys(tickerMeta).sort();

  body.innerHTML = '';

  // THEMES section
  {
    const sec = document.createElement('div');
    sec.innerHTML = '<div class="conv-section-label">THEMES</div>';
    const list = document.createElement('div');
    list.style.cssText = 'display:flex;flex-direction:column;gap:8px;';
    for (const [key, theme] of Object.entries(themes)) {
      list.appendChild(buildThemeCard(key, theme));
    }
    sec.appendChild(list);

    // Add theme row
    const addRow = document.createElement('div');
    addRow.className = 'conv-add-theme-row';
    addRow.innerHTML = `
      <input class="conv-add-theme-input" id="conv-new-theme-key" placeholder="theme_name (snake_case)" spellcheck="false">
      <button class="conv-add-theme-btn" id="conv-new-theme-btn">+ ADD THEME</button>
    `;
    addRow.querySelector('#conv-new-theme-btn').addEventListener('click', () => {
      const inp = addRow.querySelector('#conv-new-theme-key');
      addTheme(inp.value.trim().toLowerCase().replace(/\s+/g, '_').replace(/[^a-z_]/g, ''));
      inp.value = '';
    });
    addRow.querySelector('#conv-new-theme-key').addEventListener('keydown', e => {
      if (e.key === 'Enter') addRow.querySelector('#conv-new-theme-btn').click();
    });
    sec.appendChild(addRow);
    body.appendChild(sec);
  }

  // TICKER INTENT section
  if (allMetaTickers.length) {
    const sec = document.createElement('div');
    sec.innerHTML = '<div class="conv-section-label">TICKER INTENT</div>';
    const grid = document.createElement('div');
    grid.style.cssText = 'display:flex;flex-direction:column;gap:2px;';
    for (const ticker of allMetaTickers) {
      grid.appendChild(buildIntentRow(ticker, tickerMeta[ticker] || {}));
    }
    sec.appendChild(grid);
    body.appendChild(sec);
  }

  // INDIVIDUAL HOLDINGS section
  const holdingEntries = Object.entries(holdings);
  if (holdingEntries.length) {
    const sec = document.createElement('div');
    sec.innerHTML = `
      <div class="conv-section-label">INDIVIDUAL HOLDINGS</div>
      <div style="font-family:var(--font-mono);font-size:10px;color:var(--text-dim);letter-spacing:0.5px;margin-bottom:12px;line-height:1.6;">
        Positions held for individual reasons outside of a conviction theme.
        All are <span style="color:var(--text-muted)">HOLD</span> — intent and profit-take rules are managed via Ticker Intent above.
      </div>`;
    const list = document.createElement('div');
    list.style.cssText = 'display:flex;flex-direction:column;gap:6px;';
    for (const [ticker, h] of holdingEntries) {
      const item = document.createElement('div');
      item.className = 'conv-holding-item';
      const notes = DOMPurify.sanitize(h.thesis || '');
      const actionNote = DOMPurify.sanitize(h.action || '');
      item.innerHTML = `
        <div class="conv-holding-top">
          <span class="conv-holding-ticker">${ticker}</span>
          <span class="conv-holding-status">${(h.status || 'hold').replace(/_/g,' ').toUpperCase()}</span>
        </div>
        ${notes ? `<div class="conv-holding-notes">${notes}</div>` : ''}
        ${actionNote ? `<div class="conv-holding-action">${actionNote}</div>` : ''}
      `;
      list.appendChild(item);
    }
    sec.appendChild(list);
    body.appendChild(sec);
  }
}

function buildThemeCard(key, theme) {
  const card = document.createElement('div');
  card.className = 'conv-theme-card';

  const approvedList  = theme.approved   || [];
  const preferredList = theme.preferred  || [];
  const holdList      = theme.hold_only  || [];
  const dontList      = theme.do_not_add || [];
  const totalTickers  = approvedList.length + preferredList.length + holdList.length + dontList.length;
  const conv = theme.conviction || 'low';

  const header = document.createElement('div');
  header.className = 'conv-theme-header';
  header.innerHTML = `
    <span class="conv-theme-chevron">▶</span>
    <span class="conv-theme-name">${key.replace(/_/g,' ')}</span>
    <span class="conv-theme-ticker-count">${totalTickers} tickers</span>
    <span class="lane-conviction conviction-${conv}" style="font-size:8px;padding:2px 8px;">
      ${conv.replace('_',' ').toUpperCase()}
    </span>
    <button class="conv-theme-delete" title="Delete theme" data-key="${key}">✕</button>
  `;
  card.appendChild(header);

  header.querySelector('.conv-theme-delete').addEventListener('click', e => {
    e.stopPropagation();
    deleteTheme(key);
  });

  const body = document.createElement('div');
  body.className = 'conv-theme-body';

  // Conviction segmented
  const convRow = document.createElement('div');
  convRow.className = 'conv-conviction-row';
  convRow.innerHTML = '<div class="conv-field-label">CONVICTION</div>';
  const seg = document.createElement('div');
  seg.className = 'conv-segmented';
  for (const lvl of CONVICTION_LEVELS) {
    const btn = document.createElement('button');
    btn.className = 'conv-seg-btn' + (conv === lvl ? ' active' : '');
    btn.dataset.val = lvl;
    btn.textContent = CONVICTION_LABELS[lvl];
    btn.addEventListener('click', () => {
      seg.querySelectorAll('.conv-seg-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      _convs.themes[key].conviction = lvl;
      const badge = card.querySelector('.lane-conviction');
      badge.className = `lane-conviction conviction-${lvl}`;
      badge.textContent = lvl.replace('_',' ').toUpperCase();
      badge.style.cssText = 'font-size:8px;padding:2px 8px;';
      scheduleAutosave();
    });
    seg.appendChild(btn);
  }
  convRow.appendChild(seg);
  body.appendChild(convRow);

  // Thesis — bullet view with edit toggle
  const thesisDiv = document.createElement('div');
  thesisDiv.className = 'conv-field';

  const labelRow = document.createElement('div');
  labelRow.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:7px;';
  const fieldLabel = document.createElement('div');
  fieldLabel.className = 'conv-field-label';
  fieldLabel.style.marginBottom = '0';
  fieldLabel.textContent = 'THESIS';
  const editToggle = document.createElement('button');
  editToggle.className = 'conv-edit-toggle';
  editToggle.textContent = 'EDIT';
  labelRow.appendChild(fieldLabel);
  labelRow.appendChild(editToggle);
  thesisDiv.appendChild(labelRow);

  function parseThesisBullets(text) {
    if (!text) return [];
    if (text.includes('\n')) return text.split('\n').map(s => s.trim()).filter(Boolean);
    return text.split(/\.\s+/).map(s => s.trim()).filter(Boolean)
      .map(s => s.endsWith('.') ? s.slice(0,-1) : s);
  }

  let bullets = parseThesisBullets(theme.thesis || '');

  const ul = document.createElement('ul');
  ul.className = 'conv-thesis-list';
  function rebuildList() {
    ul.innerHTML = '';
    if (bullets.length) {
      bullets.forEach(b => { const li = document.createElement('li'); li.textContent = b; ul.appendChild(li); });
    } else {
      const li = document.createElement('li'); li.textContent = 'No thesis yet — click EDIT to add.';
      li.style.color = 'var(--text-dim)'; li.style.fontStyle = 'italic'; ul.appendChild(li);
    }
  }
  rebuildList();
  thesisDiv.appendChild(ul);

  const ta = document.createElement('textarea');
  ta.className = 'conv-textarea';
  ta.style.display = 'none';
  ta.rows = 5;
  ta.placeholder = 'One bullet per line…';
  ta.value = bullets.join('\n');
  ta.addEventListener('blur', () => {
    bullets = ta.value.split('\n').map(s => s.trim()).filter(Boolean);
    _convs.themes[key].thesis = bullets.join('\n');
    rebuildList();
    ta.style.display = 'none';
    ul.style.display = '';
    editToggle.textContent = 'EDIT';
    scheduleAutosave();
  });
  thesisDiv.appendChild(ta);

  editToggle.addEventListener('click', () => {
    if (ta.style.display === 'none') {
      ta.value = bullets.join('\n');
      ta.style.display = 'block';
      ul.style.display = 'none';
      editToggle.textContent = 'DONE';
      ta.focus();
    } else {
      ta.blur();
    }
  });

  body.appendChild(thesisDiv);

  // Ticker chip sections
  const chipDefs = [
    { label: 'APPROVED',   field: 'approved',   cls: 'approved'   },
    { label: 'PREFERRED',  field: 'preferred',  cls: 'approved'   },
    { label: 'HOLD ONLY',  field: 'hold_only',  cls: 'hold-only'  },
    { label: 'DO NOT ADD', field: 'do_not_add', cls: 'do-not-add' },
  ];

  for (const cd of chipDefs) {
    const currentList = () => _convs.themes[key][cd.field] || [];
    if (!currentList().length && cd.field !== 'approved') continue;

    const sec = document.createElement('div');
    sec.className = 'conv-ticker-list-section';
    sec.innerHTML = `<div class="conv-ticker-list-label">${cd.label}</div>`;
    const chips = document.createElement('div');
    chips.className = 'conv-chips';

    function rebuildChips(chipsEl, def) {
      chipsEl.innerHTML = '';
      for (const t of (currentList())) {
        const chip = document.createElement('span');
        chip.className = `conv-chip ${def.cls}`;
        chip.innerHTML = `${t}<button class="conv-chip-remove" title="Remove">✕</button>`;
        chip.querySelector('.conv-chip-remove').addEventListener('click', () => {
          _convs.themes[key][def.field] = (_convs.themes[key][def.field] || []).filter(x => x !== t);
          rebuildChips(chipsEl, def);
          scheduleAutosave();
        });
        chipsEl.appendChild(chip);
      }
      // Add input
      const inp = document.createElement('input');
      inp.type = 'text'; inp.className = 'conv-add-input'; inp.placeholder = '+ ADD';
      inp.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
          const val = inp.value.trim().toUpperCase();
          if (val && !currentList().includes(val)) {
            if (!_convs.themes[key][def.field]) _convs.themes[key][def.field] = [];
            _convs.themes[key][def.field].push(val);
            rebuildChips(chipsEl, def);
            scheduleAutosave();
          } else { inp.value = ''; }
        }
        if (e.key === 'Escape') inp.blur();
      });
      chipsEl.appendChild(inp);
    }

    rebuildChips(chips, cd);
    sec.appendChild(chips);
    body.appendChild(sec);
  }

  card.appendChild(body);
  header.addEventListener('click', () => card.classList.toggle('open'));
  return card;
}

function buildIntentRow(ticker, meta) {
  const row = document.createElement('div');
  row.className = 'conv-intent-row';

  const tickerEl = document.createElement('span');
  tickerEl.className = 'conv-intent-ticker';
  tickerEl.textContent = ticker;
  row.appendChild(tickerEl);

  const block = document.createElement('div');
  block.className = 'conv-intent-block';

  const cardsEl = document.createElement('div');
  cardsEl.className = 'conv-intent-cards';

  let currentIntent = meta.intent || 'opportunistic';

  const catalystInp = document.createElement('input');
  catalystInp.type = 'text';
  catalystInp.className = 'conv-catalyst-input';
  catalystInp.placeholder = 'Original catalyst (e.g. COVID recovery)';
  catalystInp.value = meta.original_catalyst || '';
  catalystInp.style.display = currentIntent === 'opportunistic' ? 'block' : 'none';
  catalystInp.addEventListener('blur', () => {
    if (!_convs.ticker_metadata[ticker]) _convs.ticker_metadata[ticker] = {};
    _convs.ticker_metadata[ticker].original_catalyst = catalystInp.value.trim() || null;
    scheduleAutosave();
  });

  for (const opt of INTENT_OPTIONS) {
    const btn = document.createElement('button');
    btn.className = 'conv-intent-card' + (currentIntent === opt.val ? ' active' : '');
    btn.dataset.intent = opt.val;
    btn.innerHTML = `<span style="display:block;margin-bottom:1px;font-size:11px">${opt.icon}</span><span style="display:block;font-size:8px;letter-spacing:0.5px">${opt.line1}</span>${opt.line2 ? `<span style="display:block;font-size:8px;letter-spacing:0.5px">${opt.line2}</span>` : ''}`;
    btn.addEventListener('click', () => {
      cardsEl.querySelectorAll('.conv-intent-card').forEach(c => c.classList.remove('active'));
      btn.classList.add('active');
      currentIntent = opt.val;
      if (!_convs.ticker_metadata) _convs.ticker_metadata = {};
      if (!_convs.ticker_metadata[ticker]) _convs.ticker_metadata[ticker] = {};
      _convs.ticker_metadata[ticker].intent = opt.val;
      if (opt.val !== 'opportunistic') {
        _convs.ticker_metadata[ticker].original_catalyst = null;
        catalystInp.value = '';
      }
      catalystInp.style.display = opt.val === 'opportunistic' ? 'block' : 'none';
      scheduleAutosave();
    });
    cardsEl.appendChild(btn);
  }

  block.appendChild(cardsEl);
  block.appendChild(catalystInp);
  row.appendChild(block);
  return row;
}

function addTheme(key) {
  if (!key) return;
  if (_convs.themes[key]) {
    alert(`Theme "${key}" already exists.`);
    return;
  }
  _convs.themes[key] = {
    thesis: '', conviction: 'medium',
    approved: [], preferred: [], hold_only: [], do_not_add: [], notes: {},
  };
  renderConvDrawer();
  scheduleAutosave();
  // Scroll to and expand the new card
  requestAnimationFrame(() => {
    const cards = document.querySelectorAll('.conv-theme-card');
    const last = cards[cards.length - 2]; // last before add row
    if (last) { last.classList.add('open'); last.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }
  });
}

function deleteTheme(key) {
  const theme = _convs.themes[key];
  const tickerCount = [
    ...(theme?.approved || []), ...(theme?.preferred || []),
    ...(theme?.hold_only || []), ...(theme?.do_not_add || []),
  ].length;
  const msg = tickerCount > 0
    ? `Delete theme "${key}"?\n\nThis will remove ${tickerCount} ticker(s) from their theme. They'll appear in the Other lane until reassigned. Ticker intent settings are preserved.`
    : `Delete theme "${key}"?`;
  if (!confirm(msg)) return;
  delete _convs.themes[key];
  renderConvDrawer();
  scheduleAutosave();
}

function initConvictionsDrawer() {
  document.getElementById('convictions-btn').addEventListener('click', openConvictionsDrawer);
  document.getElementById('conv-close').addEventListener('click', closeConvictionsDrawer);
  document.getElementById('conv-backdrop').addEventListener('click', closeConvictionsDrawer);
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !document.getElementById('conv-drawer').classList.contains('hidden')) {
      closeConvictionsDrawer();
    }
  });
}

// ── Playbook Drawer ──────────────────────────────────────────
// Edit the Playbook — the strategies the contextual layer reasons over. Mirrors the
// convictions drawer exactly: a single-row JSONB blob, autosaved on any edit (UPDATE plays
// + INSERT plays_history snapshot + logged user_action). `plays` is anon-read + owner-rw.

let _plays = null;         // working copy of the plays blob { plays: [...] }
let _playsRowId = null;
let _playsSaveTimer = null;

const PLAY_TYPES = ['quant', 'judgment', 'hybrid'];

// Plain-language help for each known play param — what it means, how it changes ASTRA's
// behavior, and its sane range (hard bounds the backend enforces noted where relevant).
const PARAM_META = {
  beta_min: {
    desc: "Minimum market-beta for the play to fire. Beta = how much a stock moves with the market (1.0 = in step, >1 = amplified). A higher floor means only names that genuinely track the market qualify — so their drop is truly market-driven, not their own story.",
    range: "0.5 – 2.0", def: "0.8",
  },
  residual_max_pct: {
    desc: "Max company-specific residual (%). The slice of the drop the market does NOT explain — i.e. weakness that's about this company. Lower = the play insists the fall is macro noise, not a broken business.",
    range: "0 – 15%", def: "5%",
  },
  min_drop_pct: {
    desc: "Minimum recent drop (%) for a name to count as a dip worth sizing into. Below this the discount is too small to bother acting on.",
    range: "3 – 25%", def: "8%",
  },
  min_r2: {
    desc: "Reliability floor for the beta read (0–1) — how much of the stock's moves the market actually explains. Below the floor, the 'this is macro' read is untrustworthy and the play stays silent. Backend won't trust reads below 0.35.",
    range: "0 – 1", def: "0.35",
  },
  size_mult_max: {
    desc: "Biggest lot-size multiplier this play can request (1.0 = no change, 1.5 = buy 50% more into the dip). The backend hard-caps every play at 2.0 regardless of what you set here.",
    range: "1.0 – 2.0", def: "2.0",
  },
  nudge_max: {
    desc: "Largest upward conviction bump this play may request, used to tip a near-miss WATCH into a BUY on a clean deep dip. The backend hard-caps this at 0.10.",
    range: "0 – 0.10", def: "0.10",
  },
};

// A single reusable, fixed-position tooltip so param help never clips inside the scrolling
// drawer. Shown on hover/focus of any .pb-param-info icon; content pulled from PARAM_META.
let _pbTip = null;
function ensurePbTooltip() {
  if (_pbTip) return _pbTip;
  _pbTip = document.createElement('div');
  _pbTip.id = 'pb-tip';
  _pbTip.className = 'pb-tip hidden';
  document.body.appendChild(_pbTip);
  return _pbTip;
}
function showPbTooltip(icon) {
  const key = icon.dataset.param;
  const meta = PARAM_META[key];
  const tip = ensurePbTooltip();
  tip.innerHTML = meta
    ? `<div class="pb-tip-title">${key}</div>
       <div class="pb-tip-desc">${meta.desc}</div>
       <div class="pb-tip-range"><span>RANGE</span> ${meta.range} <span class="pb-tip-def">· default ${meta.def}</span></div>`
    : `<div class="pb-tip-title">${key}</div>
       <div class="pb-tip-desc">Custom parameter — no built-in description. The backend clamps it to its own safe bounds at run time.</div>`;
  tip.classList.remove('tuning-tip');   // shared element — drop the Tuning accent
  tip.classList.remove('hidden');
  positionPbTip(tip, icon);
}

// Position a floating tip above its icon, centred, flipping below if there isn't room.
// Shared by the Playbook param tips and the Tuning drawer.
function positionPbTip(tip, icon) {
  const r = icon.getBoundingClientRect();
  const tr = tip.getBoundingClientRect();
  let left = r.left + r.width / 2 - tr.width / 2;
  left = Math.max(10, Math.min(left, window.innerWidth - tr.width - 10));
  let top = r.top - tr.height - 10;
  tip.classList.toggle('pb-tip-below', top < 10);
  if (top < 10) top = r.bottom + 10;
  tip.style.left = `${left}px`;
  tip.style.top = `${top}px`;
}
function hidePbTooltip() {
  if (_pbTip) _pbTip.classList.add('hidden');
}

function setPlaysSaveStatus(state, msg) {
  const el = document.getElementById('playbook-save-status');
  if (!el) return;
  el.className = 'conv-save-status ' + (state || '');
  el.textContent = msg || '';
}

function schedulePlaysAutosave() {
  setPlaysSaveStatus('saving', '● Saving…');
  clearTimeout(_playsSaveTimer);
  _playsSaveTimer = setTimeout(doSavePlays, 900);
}

async function doSavePlays() {
  if (!_sb || !_plays || !_playsRowId) return;
  try {
    const { error } = await _sb.from('plays')
      .update({ content: _plays, updated_at: new Date().toISOString(), updated_by: 'dashboard' })
      .eq('id', _playsRowId);
    if (error) throw error;
    // Historize the edit like the Python save path (plays_history) + log the event.
    _sb.from('plays_history')
      .insert({ content: _plays, saved_at: new Date().toISOString() })
      .then(({ error: histErr }) => {
        if (histErr) console.error('plays_history append failed:', histErr.message);
      });
    logUserAction('playbook_edit', null, {});
    setPlaysSaveStatus('saved', '● Saved just now');
    setTimeout(() => setPlaysSaveStatus('', ''), 3000);
  } catch (err) {
    setPlaysSaveStatus('error', '● Save failed');
    console.error('Playbook save error:', err.message);
    if (err.code === '42501' || (err.message || '').includes('policy')) {
      console.warn(
        'Add this in Supabase SQL Editor:\n' +
        "CREATE POLICY \"owner rw plays\" ON plays\n" +
        "  FOR ALL TO authenticated USING (auth.email() = 'abhikirk@icloud.com');\n" +
        "GRANT ALL ON plays TO authenticated;"
      );
    }
  }
}

function openPlaybookDrawer() {
  document.getElementById('playbook-backdrop').classList.remove('hidden');
  document.getElementById('playbook-drawer').classList.remove('hidden');
  document.body.style.overflow = 'hidden';
  loadAndRenderPlays();
}

function closePlaybookDrawer() {
  hidePbTooltip();
  document.getElementById('playbook-backdrop').classList.add('hidden');
  document.getElementById('playbook-drawer').classList.add('hidden');
  document.body.style.overflow = '';
}

async function loadAndRenderPlays() {
  const body = document.getElementById('playbook-body');
  body.innerHTML = '<div class="conv-loading">Loading playbook…</div>';
  try {
    const { data, error } = await _sb
      .from('plays')
      .select('id, content')
      .order('id', { ascending: false })
      .limit(1)
      .single();
    if (error) throw error;
    _plays = JSON.parse(JSON.stringify(data.content));
    if (!Array.isArray(_plays.plays)) _plays.plays = [];
    _playsRowId = data.id;
    renderPlaysDrawer();
  } catch (err) {
    body.innerHTML = `<div class="conv-loading" style="color:var(--accent-red)">Failed to load: ${err.message}</div>`;
  }
}

function renderPlaysDrawer() {
  const body = document.getElementById('playbook-body');
  body.innerHTML = '';

  const sec = document.createElement('div');
  sec.innerHTML = '<div class="conv-section-label">PLAYS</div>' +
    '<div class="pb-drawer-intro">Strategies the contextual reasoning layer evaluates each run. ' +
    '<b>quant</b> = deterministic code evaluator · <b>judgment</b> = LLM-only · <b>hybrid</b> = quant arms it, the LLM confirms/vetoes. ' +
    'Params tune within the hard bounds the code enforces.</div>';
  const list = document.createElement('div');
  list.style.cssText = 'display:flex;flex-direction:column;gap:10px;';
  _plays.plays.forEach((play, i) => list.appendChild(buildPlayCard(play, i)));
  sec.appendChild(list);

  // Add play row
  const addRow = document.createElement('div');
  addRow.className = 'conv-add-theme-row';
  addRow.innerHTML = `
    <input class="conv-add-theme-input" id="pb-new-play-key" placeholder="play_name (snake_case)" spellcheck="false">
    <button class="conv-add-theme-btn" id="pb-new-play-btn">+ ADD PLAY</button>
  `;
  addRow.querySelector('#pb-new-play-btn').addEventListener('click', () => {
    const inp = addRow.querySelector('#pb-new-play-key');
    addPlay(inp.value.trim().toLowerCase().replace(/\s+/g, '_').replace(/[^a-z0-9_]/g, ''));
    inp.value = '';
  });
  addRow.querySelector('#pb-new-play-key').addEventListener('keydown', e => {
    if (e.key === 'Enter') addRow.querySelector('#pb-new-play-btn').click();
  });
  sec.appendChild(addRow);
  body.appendChild(sec);
}

function addPlay(name) {
  if (!name) return;
  if (_plays.plays.some(p => p.name === name)) { setPlaysSaveStatus('error', '● Play already exists'); return; }
  _plays.plays.push({
    name, enabled: true, type: 'judgment', weight: 1.0,
    rationale: '', evaluates: '', params: {},
  });
  renderPlaysDrawer();
  schedulePlaysAutosave();
}

function deletePlay(name) {
  _plays.plays = _plays.plays.filter(p => p.name !== name);
  renderPlaysDrawer();
  schedulePlaysAutosave();
}

function buildPlayCard(play, idx) {
  const card = document.createElement('div');
  card.className = 'conv-theme-card pb-play-card';

  const header = document.createElement('div');
  header.className = 'conv-theme-header';
  header.innerHTML = `
    <span class="conv-theme-chevron">▶</span>
    <span class="conv-theme-name">${play.name.replace(/_/g, ' ')}</span>
    <span class="pb-type-badge pb-type-${play.type}">${(play.type || '').toUpperCase()}</span>
    <label class="pb-enable-toggle" title="Enable / disable this play">
      <input type="checkbox" ${play.enabled ? 'checked' : ''}>
      <span class="pb-enable-slider"></span>
    </label>
    <button class="conv-theme-delete" title="Delete play" data-name="${play.name}">✕</button>
  `;
  card.appendChild(header);

  header.addEventListener('click', e => {
    if (e.target.closest('.pb-enable-toggle') || e.target.closest('.conv-theme-delete')) return;
    card.classList.toggle('open');
  });
  header.querySelector('.conv-theme-delete').addEventListener('click', e => {
    e.stopPropagation();
    deletePlay(play.name);
  });
  header.querySelector('.pb-enable-toggle input').addEventListener('change', e => {
    _plays.plays[idx].enabled = e.target.checked;
    schedulePlaysAutosave();
  });

  const cbody = document.createElement('div');
  cbody.className = 'conv-theme-body';

  // TYPE segmented
  const typeRow = document.createElement('div');
  typeRow.className = 'conv-conviction-row';
  typeRow.innerHTML = '<div class="conv-field-label">TYPE</div>';
  const seg = document.createElement('div');
  seg.className = 'conv-segmented';
  for (const t of PLAY_TYPES) {
    const btn = document.createElement('button');
    btn.className = 'conv-seg-btn' + (play.type === t ? ' active' : '');
    btn.dataset.val = t;
    btn.textContent = t.toUpperCase();
    btn.addEventListener('click', () => {
      seg.querySelectorAll('.conv-seg-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      _plays.plays[idx].type = t;
      const badge = header.querySelector('.pb-type-badge');
      badge.className = `pb-type-badge pb-type-${t}`;
      badge.textContent = t.toUpperCase();
      schedulePlaysAutosave();
    });
    seg.appendChild(btn);
  }
  typeRow.appendChild(seg);
  cbody.appendChild(typeRow);

  // WEIGHT
  const wRow = document.createElement('div');
  wRow.className = 'pb-field';
  wRow.innerHTML = '<div class="conv-field-label">WEIGHT <span class="pb-field-hint">0–1 · scales this play\'s influence</span></div>';
  const wInput = document.createElement('input');
  wInput.type = 'number'; wInput.step = '0.05'; wInput.min = '0'; wInput.max = '1';
  wInput.className = 'pb-num-input';
  wInput.value = play.weight ?? 1.0;
  wInput.addEventListener('input', () => {
    const v = Math.max(0, Math.min(1, parseFloat(wInput.value) || 0));
    _plays.plays[idx].weight = v;
    schedulePlaysAutosave();
  });
  wRow.appendChild(wInput);
  cbody.appendChild(wRow);

  // RATIONALE
  cbody.appendChild(buildPlayTextField('RATIONALE', 'Why this play exists (human-facing).', play.rationale || '', v => {
    _plays.plays[idx].rationale = v; schedulePlaysAutosave();
  }));
  // EVALUATES (the LLM reasons over this prose)
  cbody.appendChild(buildPlayTextField('EVALUATES', 'When the play fires — the LLM reasons over this.', play.evaluates || '', v => {
    _plays.plays[idx].evaluates = v; schedulePlaysAutosave();
  }));

  // PARAMS
  const pRow = document.createElement('div');
  pRow.className = 'pb-field';
  pRow.innerHTML = '<div class="conv-field-label">PARAMS <span class="pb-field-hint">numeric knobs · tune within code-enforced bounds</span></div>';
  const grid = document.createElement('div');
  grid.className = 'pb-params-grid';
  const params = _plays.plays[idx].params = _plays.plays[idx].params || {};
  const renderParams = () => {
    grid.innerHTML = '';
    for (const [k, v] of Object.entries(params)) {
      const p = document.createElement('div');
      p.className = 'pb-param';
      const key = document.createElement('span');
      key.className = 'pb-param-key';
      const name = document.createElement('span');
      name.className = 'pb-param-name'; name.textContent = k;
      const info = document.createElement('button');
      info.className = 'pb-param-info'; info.type = 'button';
      info.dataset.param = k; info.textContent = 'ⓘ';
      info.setAttribute('aria-label', `What ${k} means`);
      info.addEventListener('mouseenter', () => showPbTooltip(info));
      info.addEventListener('mouseleave', hidePbTooltip);
      info.addEventListener('focus', () => showPbTooltip(info));
      info.addEventListener('blur', hidePbTooltip);
      info.addEventListener('click', e => e.preventDefault());
      key.appendChild(name); key.appendChild(info);
      const val = document.createElement('input');
      val.type = 'number'; val.step = 'any'; val.className = 'pb-num-input pb-param-val';
      val.value = v;
      val.addEventListener('input', () => {
        const nv = parseFloat(val.value);
        params[k] = isNaN(nv) ? val.value : nv;
        schedulePlaysAutosave();
      });
      const del = document.createElement('button');
      del.className = 'pb-param-del'; del.textContent = '✕'; del.title = 'Remove param';
      del.addEventListener('click', () => { delete params[k]; renderParams(); schedulePlaysAutosave(); });
      p.appendChild(key); p.appendChild(val); p.appendChild(del);
      grid.appendChild(p);
    }
  };
  renderParams();
  pRow.appendChild(grid);

  const addParam = document.createElement('div');
  addParam.className = 'pb-add-param-row';
  addParam.innerHTML = `
    <input class="pb-add-param-key" placeholder="param_name" spellcheck="false">
    <input class="pb-add-param-val" type="number" step="any" placeholder="value">
    <button class="pb-add-param-btn">+</button>
  `;
  addParam.querySelector('.pb-add-param-btn').addEventListener('click', () => {
    const kEl = addParam.querySelector('.pb-add-param-key');
    const vEl = addParam.querySelector('.pb-add-param-val');
    const k = kEl.value.trim().toLowerCase().replace(/\s+/g, '_').replace(/[^a-z0-9_]/g, '');
    const v = parseFloat(vEl.value);
    if (!k || isNaN(v)) { setPlaysSaveStatus('error', '● Param needs a name + number'); return; }
    params[k] = v;
    kEl.value = ''; vEl.value = '';
    renderParams();
    schedulePlaysAutosave();
  });
  pRow.appendChild(addParam);
  cbody.appendChild(pRow);

  card.appendChild(cbody);
  return card;
}

function buildPlayTextField(label, hint, value, onChange) {
  const wrap = document.createElement('div');
  wrap.className = 'pb-field';
  wrap.innerHTML = `<div class="conv-field-label">${label} <span class="pb-field-hint">${hint}</span></div>`;
  const ta = document.createElement('textarea');
  ta.className = 'pb-textarea';
  ta.value = value;
  ta.rows = Math.max(2, Math.min(6, Math.ceil((value.length || 40) / 60)));
  ta.addEventListener('input', () => onChange(ta.value));
  wrap.appendChild(ta);
  return wrap;
}

function initPlaybookDrawer() {
  document.getElementById('playbook-btn').addEventListener('click', openPlaybookDrawer);
  document.getElementById('playbook-close').addEventListener('click', closePlaybookDrawer);
  document.getElementById('playbook-backdrop').addEventListener('click', closePlaybookDrawer);
  document.getElementById('playbook-body').addEventListener('scroll', hidePbTooltip);
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !document.getElementById('playbook-drawer').classList.contains('hidden')) {
      closePlaybookDrawer();
    }
  });
}

// ── Human-action event log (user_actions) ─────────────────────
// Append-only timeline of the owner's control/feedback actions (pause/resume, ratings,
// journal feedback, conviction edits, exploration rejects) — captured as events for future
// ML training, not just as state overwrites. Non-fatal: a logging failure must never block
// the underlying action. Uses whichever authenticated client is available.
async function logUserAction(actionType, target, payload) {
  const client = _journalSb || _sb;
  if (!client) return;
  try {
    await client.from('user_actions').insert({
      action_type: actionType,
      target: target ?? null,
      payload: payload ?? {},
    });
  } catch (err) {
    console.error('user_actions log failed:', err?.message || err);
  }
}

// ── Trade Journal Drawer ──────────────────────────────────────

let _journalSb = null;
let _pendingJournalCount = 0;

function openJournalDrawer() {
  document.getElementById('journal-backdrop').classList.remove('hidden');
  document.getElementById('journal-drawer').classList.remove('hidden');
  document.body.style.overflow = 'hidden';
  loadAndRenderJournal();
}

function closeJournalDrawer() {
  document.getElementById('journal-backdrop').classList.add('hidden');
  document.getElementById('journal-drawer').classList.add('hidden');
  document.body.style.overflow = '';
}

async function loadJournalBadge(sb) {
  if (!isAuthenticated) return;
  _journalSb = sb;
  try {
    const { data, error } = await sb
      .from('user_trades_log')
      .select('id')
      .eq('feedback_status', 'pending');
    if (error) { console.error('[Journal badge] query error:', error); return; }
    const count = data?.length ?? 0;
    _pendingJournalCount = count;
    updateJournalBadge(count);
    const btn = document.getElementById('journal-btn');
    if (btn) btn.classList.remove('hidden');
  } catch { /* silent */ }
}

function updateJournalBadge(count) {
  const badge = document.getElementById('journal-badge');
  const btn   = document.getElementById('journal-btn');
  if (!badge || !btn) return;
  if (count > 0) {
    badge.textContent = count > 9 ? '9+' : String(count);
    badge.classList.remove('hidden');
    btn.classList.add('has-pending');
  } else {
    badge.classList.add('hidden');
    btn.classList.remove('has-pending');
  }
}

async function loadAndRenderJournal() {
  const body = document.getElementById('journal-body');
  body.innerHTML = '<div class="conv-loading">Loading trade history…</div>';

  try {
    const { data, error } = await _journalSb
      .from('user_trades_log')
      .select('*')
      .eq('feedback_status', 'pending')
      .order('detected_at', { ascending: false });
    if (error) throw error;
    // Reconcile the header badge with the authoritative pending count — self-heals a
    // stale count left behind when an item was resolved in another tab/device or expired.
    _pendingJournalCount = (data || []).length;
    updateJournalBadge(_pendingJournalCount);
    renderJournalDrawer(data || []);
  } catch (err) {
    body.innerHTML = `<div class="conv-loading" style="color:var(--accent-red)">Failed to load: ${err.message}</div>`;
  }
}

function renderJournalDrawer(items) {
  const body = document.getElementById('journal-body');
  body.innerHTML = '';

  if (!items.length) {
    body.innerHTML = `<div class="journal-empty">
      No pending trade reviews.<br>
      <span style="font-size:11px;opacity:0.5">Trades are auto-detected after each daily run.</span>
    </div>`;
    return;
  }

  items.forEach(item => {
    const card = buildJournalCard(item);
    body.appendChild(card);
  });

  const note = document.createElement('div');
  note.className = 'journal-ttl-note';
  note.textContent = 'Pending items expire after 30 days';
  body.appendChild(note);
}

function buildJournalCard(item) {
  const card = document.createElement('div');
  card.className = 'journal-card';

  const tradeDate  = item.trade_date ? new Date(item.trade_date + 'T12:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : '—';
  const actionCls  = item.action === 'buy' ? 'buy' : 'sell';
  const suspCls    = item.astra_suspicion ? 'astra' : 'manual';
  const suspIcon   = item.astra_suspicion ? '⚡' : '◦';
  const suspText   = item.astra_suspicion_reason || '';
  const priceStr   = item.price_estimated ? ` · ~$${Number(item.price_estimated).toFixed(2)}` : '';

  card.innerHTML = `
    <div class="journal-card-header">
      <span class="journal-card-date">${tradeDate}${priceStr}</span>
      <span class="journal-card-ticker">${item.ticker}</span>
      <span class="journal-card-action ${actionCls}">${item.action}</span>
    </div>
    <div class="journal-suspicion ${suspCls}">
      <span class="journal-suspicion-icon">${suspIcon}</span>${suspText}
    </div>
    <div class="journal-attribution">
      <button class="journal-attr-btn" data-value="astra">ASTRA Recommendation</button>
      <button class="journal-attr-btn" data-value="manual">My own call</button>
    </div>
    <input class="journal-reason-input" type="text" maxlength="120"
      placeholder="Why this trade? (optional, ~100 chars)" spellcheck="true">
    <button class="journal-submit-btn" disabled>SUBMIT →</button>
  `;

  const attrBtns  = card.querySelectorAll('.journal-attr-btn');
  const reasonEl  = card.querySelector('.journal-reason-input');
  const submitBtn = card.querySelector('.journal-submit-btn');
  let selectedAttr = null;

  attrBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      attrBtns.forEach(b => b.classList.remove('selected', 'astra', 'manual'));
      btn.classList.add('selected', btn.dataset.value);
      selectedAttr = btn.dataset.value;
      submitBtn.disabled = false;
    });
  });

  submitBtn.addEventListener('click', async () => {
    submitBtn.disabled = true;
    submitBtn.textContent = 'Saving…';
    await submitTradeJournalEntry(item, selectedAttr === 'astra', reasonEl.value.trim(), card);
  });

  return card;
}

async function submitTradeJournalEntry(item, fromAstra, reason, card) {
  try {
    const now = new Date().toISOString();
    const { error } = await _journalSb
      .from('user_trades_log')
      .update({
        from_astra_recommendation: fromAstra,
        user_reason: reason || null,
        feedback_status: 'submitted',
        feedback_given_at: now,
      })
      .eq('id', item.id);
    if (error) throw error;

    // If user confirmed an ASTRA recommendation, update the decisions row too
    if (fromAstra && item.astra_signal_id) {
      await _journalSb
        .from('decisions')
        .update({ user_acted: true, acted_at: now })
        .eq('id', item.astra_signal_id);
    } else if (!fromAstra && item.astra_signal_id) {
      await _journalSb
        .from('decisions')
        .update({ user_acted: false })
        .eq('id', item.astra_signal_id);
    }

    logUserAction('trade_feedback', item.ticker, {
      from_astra: fromAstra, reason: reason || null,
      trade_id: item.id, astra_signal_id: item.astra_signal_id ?? null,
    });

    // Animate card out
    card.style.transition = 'opacity 0.25s, transform 0.25s';
    card.style.opacity = '0';
    card.style.transform = 'translateX(20px)';
    setTimeout(() => {
      card.remove();
      _pendingJournalCount = Math.max(0, _pendingJournalCount - 1);
      updateJournalBadge(_pendingJournalCount);
      // Show empty state if no cards left
      const body = document.getElementById('journal-body');
      if (!body.querySelector('.journal-card')) {
        body.innerHTML = `<div class="journal-empty">All caught up!<br>
          <span style="font-size:11px;opacity:0.5">New trades will appear here after each daily run.</span>
        </div>`;
      }
    }, 280);
  } catch (err) {
    const submitBtn = card.querySelector('.journal-submit-btn');
    submitBtn.textContent = 'Error — retry';
    submitBtn.disabled = false;
    console.error('Journal submit error:', err);
  }
}

function initJournalDrawer() {
  document.getElementById('journal-btn').addEventListener('click', openJournalDrawer);
  document.getElementById('journal-close').addEventListener('click', closeJournalDrawer);
  document.getElementById('journal-backdrop').addEventListener('click', closeJournalDrawer);
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !document.getElementById('journal-drawer').classList.contains('hidden')) {
      closeJournalDrawer();
    }
  });
}

// ── Advisor Note Rating ───────────────────────────────────────

function initAdvisorRating(sb, runSummaryId, existingRating) {
  if (!isAuthenticated) return;
  const row     = document.getElementById('advisor-rating-row');
  const doneEl  = document.getElementById('advisor-rating-done');
  const upBtn   = document.getElementById('advisor-rate-up');
  const downBtn = document.getElementById('advisor-rate-down');

  row.classList.remove('hidden');

  if (existingRating != null) {
    upBtn.classList.add('hidden');
    downBtn.classList.add('hidden');
    doneEl.classList.remove('hidden');
    return;
  }

  async function submitRating(rating) {
    upBtn.disabled = true; downBtn.disabled = true;
    try {
      await sb.from('run_summaries').update({ advisor_rating: rating }).eq('id', runSummaryId);
      logUserAction('advisor_rating', String(runSummaryId), { rating });
      upBtn.classList.add('hidden');
      downBtn.classList.add('hidden');
      doneEl.classList.remove('hidden');
    } catch (err) {
      upBtn.disabled = false; downBtn.disabled = false;
      console.error('Rating error:', err);
    }
  }

  upBtn.addEventListener('click',   () => submitRating(1));
  downBtn.addEventListener('click', () => submitRating(-1));
}

// ── Main render ──────────────────────────────────────────────

function showError(msg) {
  document.getElementById('loading-state').classList.add('hidden');
  const errEl = document.getElementById('error-state');
  errEl.classList.remove('hidden');
  errEl.querySelector('.error-text').textContent = '⚠ ' + msg;
}

// ── On Radar (exploration candidates) ────────────────────────

async function fetchExplorationCandidates(sb) {
  const { data } = await sb
    .from('exploration_candidates')
    .select('*')
    .in('status', ['on_radar', 'paper_trading'])
    .order('claude_conviction', { ascending: true })   // high < medium < low alphabetically — reversed below
    .order('discovered_at', { ascending: false });
  // Re-sort client-side: high → medium → low, then newest first within each tier
  const convRank = { high: 0, medium: 1, low: 2 };
  return (data || []).sort((a, b) => {
    const cr = (convRank[a.claude_conviction] ?? 1) - (convRank[b.claude_conviction] ?? 1);
    if (cr !== 0) return cr;
    return new Date(b.discovered_at) - new Date(a.discovered_at);
  });
}

function textToBullets(text, maxBullets) {
  if (!text) return '<ul class="conv-thesis-list"><li>—</li></ul>';
  const sentences = text.includes('\n')
    ? text.split('\n')
    : text.split(/(?<=\.)\s+/);
  const bullets = sentences.map(s => s.replace(/\.\s*$/, '').trim()).filter(Boolean);
  const shown = maxBullets ? bullets.slice(0, maxBullets) : bullets;
  return '<ul class="conv-thesis-list">' + shown.map(b => `<li>${b}</li>`).join('') + '</ul>';
}

function convictionColor(level) {
  if (level === 'high')   return 'var(--accent-green)';
  if (level === 'medium') return 'var(--accent-yellow, #f5c518)';
  return 'var(--text-muted)';
}

function renderOnRadar(candidates, sb) {
  const section = document.getElementById('on-radar');
  if (!candidates.length) { section.classList.add('hidden'); return; }
  section.classList.remove('hidden');

  const THEME_LABEL = {
    space:         'SPACE',
    core_tech:     'CORE TECH',
    ev_transition: 'EV',
    cannabis:      'CANNABIS',
  };

  const cards = candidates.map(c => {
    const tLabel     = THEME_LABEL[c.source_theme] || c.source_theme.toUpperCase();
    const convColor  = convictionColor(c.claude_conviction);
    const statusBadge = c.status === 'paper_trading'
      ? `<span class="radar-status-badge paper">◈ PAPER TRADING</span>`
      : `<span class="radar-status-badge on-radar">⬡ ON RADAR</span>`;

    return `
      <div class="radar-card" data-ticker="${c.ticker}" style="cursor:pointer" title="Click to expand">
        <div class="radar-card-top">
          <span class="radar-ticker">${c.ticker}</span>
          <span class="radar-theme-badge">${tLabel}</span>
          ${statusBadge}
        </div>
        <div class="radar-conviction" style="color:${convColor}">
          ASTRA CONFIDENCE: ${(c.claude_conviction || 'medium').toUpperCase()}
        </div>
        <div class="radar-rationale">${DOMPurify.sanitize(textToBullets(c.rationale, 2))}</div>
        ${isAuthenticated ? `<button class="radar-reject-btn" data-ticker="${c.ticker}">DISMISS ✕</button>` : ''}
      </div>`;
  }).join('');

  section.innerHTML = `
    <div class="section-label">
      <span class="section-icon">⬡</span>
      <span>ON RADAR</span>
      <span class="section-badge">ASTRA DISCOVERY</span>
    </div>
    <div class="radar-grid">${cards}</div>`;

  // Click card → open detail modal (but not when clicking DISMISS button)
  section.querySelectorAll('.radar-card').forEach(card => {
    card.addEventListener('click', e => {
      if (e.target.classList.contains('radar-reject-btn')) return;
      const ticker = card.dataset.ticker;
      const candidate = candidates.find(c => c.ticker === ticker);
      if (candidate) openRadarModal(candidate, THEME_LABEL);
    });
  });

  if (isAuthenticated) {
    section.querySelectorAll('.radar-reject-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const ticker = btn.dataset.ticker;
        btn.disabled = true;
        btn.textContent = 'Dismissing…';
        const { error } = await sb
          .from('exploration_candidates')
          .update({ status: 'rejected', updated_at: new Date().toISOString() })
          .eq('ticker', ticker);
        if (!error) {
          logUserAction('exploration_reject', ticker, {});
          btn.closest('.radar-card').remove();
          const remaining = section.querySelectorAll('.radar-card');
          if (!remaining.length) section.classList.add('hidden');
        } else {
          btn.disabled = false;
          btn.textContent = 'DISMISS ✕';
        }
      });
    });
  }
}

function openRadarModal(c, themeLabels) {
  const THEME_LABEL = themeLabels || {
    space: 'SPACE', core_tech: 'CORE TECH', ev_transition: 'EV', cannabis: 'CANNABIS',
  };
  const tLabel    = THEME_LABEL[c.source_theme] || (c.source_theme || '').toUpperCase();
  const convColor = convictionColor(c.claude_conviction);
  const discovered = c.discovered_at
    ? new Date(c.discovered_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
    : '—';

  document.getElementById('modal-content').innerHTML = DOMPurify.sanitize(`
    <div class="modal-header">
      <div>
        <span class="modal-ticker">${c.ticker}</span>
        <span class="modal-company">${tLabel}</span>
      </div>
      <span style="font-family:var(--font-mono);font-size:0.72rem;letter-spacing:1.5px;color:${convColor}">
        ASTRA CONFIDENCE: ${(c.claude_conviction || 'MEDIUM').toUpperCase()}
      </span>
    </div>
    <div class="modal-section-title">WHY ASTRA IS WATCHING THIS</div>
    <div style="margin:0 0 20px">${textToBullets(c.rationale)}</div>
    <div class="modal-verdict" style="grid-template-columns:1fr 1fr">
      <div class="modal-verdict-item">
        <div class="modal-verdict-label">QUALITY</div>
        <div style="margin-top:6px">${textToBullets(c.quality_summary)}</div>
      </div>
      <div class="modal-verdict-item">
        <div class="modal-verdict-label">ANALYST</div>
        <div style="margin-top:6px">${textToBullets(c.analyst_summary)}</div>
      </div>
    </div>
    <div class="modal-section-title" style="margin-top:20px">DISCOVERED BY ASTRA</div>
    <p style="margin:6px 0 0;color:var(--text-secondary)">${discovered}</p>
  `);
  document.getElementById('modal-overlay').classList.remove('hidden');
}

async function init() {
  initStarfield();

  document.getElementById('modal-close').addEventListener('click', closeModal);
  document.getElementById('modal-overlay').addEventListener('click', e => {
    if (e.target === document.getElementById('modal-overlay')) closeModal();
  });

  let sb;
  try {
    sb = createSupabaseClient();
  } catch (err) {
    showError(err.message);
    return;
  }
  _sb = sb;

  // Check existing session (Supabase auto-restores from localStorage JWT)
  const { data: { session } } = await sb.auth.getSession();
  isAuthenticated = !!session;
  updateLockBtn(isAuthenticated);
  initLockButton(sb);
  initConvictionsDrawer();
  initPlaybookDrawer();
  initJournalDrawer();
  initTuningDrawer();

  // Auth-gated header buttons
  if (isAuthenticated) {
    document.getElementById('convictions-btn').classList.remove('hidden');
    document.getElementById('playbook-btn').classList.remove('hidden');
    document.getElementById('tuning-btn').classList.remove('hidden');
    loadJournalBadge(sb);  // loads count and shows journal button
  }

  // Mission-intro scaffolding (standfirst, phase tracker, pillars, legend) is
  // for cold visitors — hide it for the owner, who already knows what ASTRA is.
  document.querySelectorAll('.intro-public').forEach(el =>
    el.classList.toggle('hidden', isAuthenticated));

  try {
    let raw, decisions = [], runDate, paperTrades = [], signalHistory = {};
    let runSummaryId = null, existingAdvisorRating = null;

    if (isAuthenticated) {
      // Full data fetch — includes raw_output, advisor_note, decisions
      const runRow = await fetchLatestRunSummary(sb);
      const rawFull = runRow.raw_output || {};
      raw = Array.isArray(rawFull) ? { signals: rawFull } : rawFull;
      runDate = runRow.run_date;
      runSummaryId = runRow.id;
      existingAdvisorRating = runRow.advisor_rating ?? null;
      [decisions, paperTrades, signalHistory] = await Promise.all([
        fetchLatestDecisions(sb, runDate),
        fetchOpenPaperTrades(sb),
        fetchRecentDecisionHistory(sb),
      ]);
      window._astraClosedPaperTrades = await fetchClosedPaperTrades(sb);
    } else {
      // Public data fetch — scrubbed public_output only, via RPC
      const { data: publicOutput, error } = await sb.rpc('get_latest_run_public');
      if (error) throw new Error(`Public data fetch failed: ${error.message}`);
      raw = publicOutput || {};
      runDate = raw.run_date;
    }

    const marketData  = raw.market_data_snapshot || {};
    const signals     = raw.signals || [];

    // Index by ticker
    const signalsByTicker   = Object.fromEntries(signals.map(s => [s.ticker, s]));
    const decisionsByTicker = Object.fromEntries(decisions.map(d => [d.ticker, d]));

    // ── Header ──
    const runDateObj = new Date(runDate);
    const ageH = (Date.now() - runDateObj.getTime()) / 3600000;
    const isFresh = ageH < 28;
    const lastRunEl = document.getElementById('last-run-date');
    lastRunEl.title = runDateObj.toLocaleString();

    // Freshness badge — inline with the value
    lastRunEl.innerHTML = `${dataAgeText(runDateObj)} <span class="freshness-badge ${isFresh ? 'fresh' : 'stale'}">${isFresh ? '● FRESH' : '● STALE'}</span>`;

    document.getElementById('next-run-date').textContent = nextWeekdayRun();
    document.getElementById('positions-count').textContent =
      raw.num_positions_screened ?? Object.keys(marketData).length;

    // ── Signals counts ──
    const counts = { buy: 0, sell: 0, watch: 0, hold: 0, blocked: 0 };
    const totalPositions = Object.keys(marketData).length;
    signals.forEach(s => { if (counts[s.action] !== undefined) counts[s.action]++; });
    counts.hold = totalPositions - counts.buy - counts.sell - counts.watch - counts.blocked;

    const heroSub = document.getElementById('hero-sub');
    const parts = [];
    if (counts.buy)  parts.push(`${counts.buy} buy signal${counts.buy !== 1 ? 's' : ''}`);
    if (counts.sell) parts.push(`${counts.sell} sell signal${counts.sell !== 1 ? 's' : ''}`);
    if (counts.watch) parts.push(`${counts.watch} on watch`);
    heroSub.textContent = parts.length
      ? parts.join(' · ') + ' across your conviction themes today'
      : 'No new signals today — holding your conviction positions';

    // ── Structured signals summary ──
    const summaryEl = document.getElementById('run-summary-text');
    const byAction = {};
    signals.forEach(s => { (byAction[s.action] = byAction[s.action] || []).push(s); });

    // A buy is only fresh/actionable as a NEW ENTRY or a cooldown-elapsed ADD; IN COOLDOWN /
    // AT ADD CAP names still clear the buy bar but are throttled by pyramiding (mirrors
    // notify._actionable_buy + the advisor-note rule) — an uptrending conviction name re-fires
    // every run, so those aren't a fresh buy today.
    function buyThrottled(sig) {
      const st = sig.buy_state;
      return !!st && !(st.startsWith('NEW ENTRY') || st.startsWith('ADD '));
    }

    function signalBadge(action, sig) {
      if (action === 'buy') {
        let html = '';
        if (sig.suggested_position_pct) {
          const pct = (sig.suggested_position_pct * 100).toFixed(0);
          html += `<span class="summary-signal-badge buy-size">${pct}%</span>`;
        }
        if (sig.buy_state) {
          const cls = buyThrottled(sig) ? 'buy-state-throttled' : 'buy-state-active';
          html += `<span class="summary-signal-badge buy-state ${cls}">${sig.buy_state}</span>`;
        }
        return html;
      }
      if (action === 'sell' && sig.reasons?.length) {
        const m = sig.reasons[0].match(/Up (\d+)%/);
        if (m) return `<span class="summary-signal-badge sell-gain">+${m[1]}%</span>`;
      }
      return '';
    }

    // Actionable-first digest: the hero surfaces what needs attention today and
    // folds the rest away (the exhaustive per-ticker view lives in Fleet Status).
    //   • BUY  — actionable entries (NEW ENTRY / ADD-eligible) shown; throttled
    //            re-fires (cooldown / at-cap) collapse into "+N on track".
    //   • SELL — all shown (every sell is actionable).
    //   • WATCH— capped, remainder behind "+N more".
    const WATCH_CAP = 6;
    const buys  = byAction.buy   || [];
    const sells = byAction.sell  || [];
    const watch = byAction.watch || [];
    const buysActive    = buys.filter(s => !buyThrottled(s));
    const buysThrottled = buys.filter(s =>  buyThrottled(s));

    function tickerUnit(action, s, throttled = false) {
      return `<span class="summary-ticker-unit${throttled ? ' buy-throttled' : ''}"><span class="ticker-link-plain" data-ticker="${s.ticker}">${s.ticker}</span>${signalBadge(action, s)}</span>`;
    }
    function moreToggle(id, label) {
      return `<button class="summary-more-toggle" type="button" data-target="${id}">${label}</button>`;
    }
    function moreBody(id, html) {
      return `<span class="summary-more-body hidden" id="${id}">${html}</span>`;
    }

    // BUY row content
    let buyInner;
    if (!buys.length) {
      buyInner = '<span class="summary-empty">—</span>';
    } else {
      const parts = [];
      if (buysActive.length) parts.push(buysActive.map(s => tickerUnit('buy', s)).join(''));
      else parts.push('<span class="summary-empty summary-empty-inline">no fresh entries</span>');
      if (buysThrottled.length) {
        parts.push(moreToggle('buy-throttled', `+${buysThrottled.length} on track`));
        parts.push(moreBody('buy-throttled', buysThrottled.map(s => tickerUnit('buy', s, true)).join('')));
      }
      buyInner = parts.join('');
    }

    // WATCH row content (cap + remainder)
    let watchInner;
    if (!watch.length) {
      watchInner = '<span class="summary-empty">—</span>';
    } else {
      const shown = watch.slice(0, WATCH_CAP);
      const rest  = watch.slice(WATCH_CAP);
      const parts = [shown.map(s => tickerUnit('watch', s)).join('')];
      if (rest.length) {
        parts.push(moreToggle('watch-more', `+${rest.length} more`));
        parts.push(moreBody('watch-more', rest.map(s => tickerUnit('watch', s)).join('')));
      }
      watchInner = parts.join('');
    }

    const summaryRows = [
      { action: 'buy',   label: '↑ BUY',   inner: buyInner },
      { action: 'sell',  label: '↓ SELL',  inner: sells.length ? sells.map(s => tickerUnit('sell', s)).join('') : '<span class="summary-empty">—</span>' },
      { action: 'watch', label: '◈ WATCH', inner: watchInner },
    ];

    summaryEl.innerHTML = `
      <div class="summary-head">
        <span class="summary-head-lab">Today's signals</span>
        <span class="summary-head-counts">
          <span class="shc buy">${buys.length}</span><span class="shc-sep">·</span><span class="shc sell">${sells.length}</span><span class="shc-sep">·</span><span class="shc watch">${watch.length}</span>
        </span>
      </div>
      <div class="summary-structured">${
        summaryRows.map(r => `
          <div class="summary-row">
            <span class="summary-action-label ${r.action}">${r.label}</span>
            <span class="summary-tickers">${r.inner}</span>
          </div>`).join('')
      }</div>`;

    // Wire the "+N" expanders (reveal the folded chips inline on the next line).
    summaryEl.querySelectorAll('.summary-more-toggle').forEach(btn => {
      btn.addEventListener('click', () => {
        const body = summaryEl.querySelector(`#${btn.dataset.target}`);
        if (!body) return;
        const open = body.classList.toggle('hidden');
        btn.classList.toggle('open', !open);
      });
    });

    // ── Advisor note ──
    const noteEl = document.getElementById('advisor-note');
    const noteText = raw.advisor_note ?? '';
    const allTickers = Object.keys(marketData);

    function renderAdvisorNote(el, text, tickers, prefixHtml = '') {
      const rendered = linkifyText(DOMPurify.sanitize(marked.parse(text)), tickers);
      // Always show title + first section; collapse from 3rd heading onward
      const headingRe = /<h[1-6][\s>]/gi;
      let m, positions = [];
      while ((m = headingRe.exec(rendered)) !== null) positions.push(m.index);
      // Primary: bold numbered section headers like **2. RISK FLAGS** inside a <p>.
      // This is the format ASTRA commonly produces.
      let splitIdx = rendered.search(/<p[^>]*>\s*<strong>2\./);
      // Secondary: numbered list items — split at the second <li> or second <ol>.
      if (splitIdx === -1) {
        const firstOlClose = rendered.indexOf('</ol>');
        const secondOl = firstOlClose !== -1 ? rendered.indexOf('<ol', firstOlClose) : -1;
        if (secondOl !== -1) splitIdx = secondOl;
      }
      if (splitIdx === -1) {
        const firstLiClose = rendered.indexOf('</li>');
        const secondLi = firstLiClose !== -1 ? rendered.indexOf('<li>', firstLiClose + 5) : -1;
        if (secondLi !== -1) splitIdx = secondLi;
      }
      // Tertiary: heading-based split (AI used ## for each section).
      if (splitIdx === -1) {
        splitIdx = positions.length >= 3 ? positions[2]
                 : positions.length >= 2 ? positions[1]
                 : -1;
      }
      // Last resort: first </p> after 200 chars.
      if (splitIdx === -1 && rendered.length > 600) {
        const idx = rendered.indexOf('</p>', 200);
        if (idx !== -1) splitIdx = idx + 4;
      }
      if (splitIdx > -1) {
        const always      = rendered.slice(0, splitIdx);
        const collapsible = rendered.slice(splitIdx);
        el.innerHTML = `
          ${prefixHtml}
          ${always}
          <button class="advisor-collapse-btn" id="advisor-toggle">
            <span>Show full analysis</span>
            <span class="advisor-collapse-arrow">▼</span>
          </button>
          <div class="advisor-collapsible" id="advisor-collapsible">${collapsible}</div>`;
        const btn  = document.getElementById('advisor-toggle');
        const body = document.getElementById('advisor-collapsible');
        btn.addEventListener('click', () => {
          const open = body.classList.toggle('open');
          btn.classList.toggle('open', open);
          btn.querySelector('span').textContent = open ? 'Hide full analysis' : 'Show full analysis';
        });
      } else {
        el.innerHTML = prefixHtml + rendered;
      }
    }

    if (!isAuthenticated) {
      noteEl.innerHTML = `<div class="advisor-locked">
        <div class="advisor-locked-icon">🔒</div>
        <div>ASTRA's weekly analysis contains personal financial data.</div>
        <button class="advisor-locked-btn" id="advisor-unlock-btn">SIGN IN TO VIEW</button>
      </div>`;
      document.getElementById('advisor-unlock-btn')?.addEventListener('click', showLoginModal);
    } else if (noteText && window.marked) {
      renderAdvisorNote(noteEl, noteText, allTickers);
    } else if (noteText) {
      noteEl.textContent = noteText;
    } else if (isAuthenticated) {
      // No note for this run — surface the most recent run that has one
      const { data: prevRuns } = await sb
        .from('run_summaries')
        .select('raw_output, run_date')
        .order('id', { ascending: false })
        .limit(10);
      const prevWithNote = (prevRuns || []).find(r => r.raw_output?.advisor_note?.trim());
      if (prevWithNote && window.marked) {
        const prevDate  = new Date(prevWithNote.run_date).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
        const prefix    = `<div class="advisor-prev-banner">Showing note from ${prevDate} — today's run was mechanical-only</div>`;
        renderAdvisorNote(noteEl, prevWithNote.raw_output.advisor_note, allTickers, prefix);
      } else {
        noteEl.innerHTML = `<p class="advisor-no-note">No advisor note available yet.</p>`;
      }
    }

    // Advisor rating (auth-gated, only when there's a current run note)
    if (isAuthenticated && runSummaryId && noteText) {
      initAdvisorRating(sb, runSummaryId, existingAdvisorRating);
    }

    // Store signal history for modal access
    window._astraSignalHistory = signalHistory;

    // ── Paper portfolio (auth-gated) ──
    const paperTradesByTicker = Object.fromEntries(paperTrades.map(pt => [pt.ticker, pt]));
    // SPY benchmark series is shared by both performance charts — fetch once.
    let spyPrices = [];
    if (isAuthenticated) {
      try {
        const [paperEquity, spy] = await Promise.all([fetchPaperEquity(sb), fetchBenchmarkPrices(sb)]);
        spyPrices = spy;
        renderPaperPortfolio(paperTrades, marketData, paperEquity, spyPrices);
      } catch (err) {
        console.error('[Paper] render error:', err);
        renderPaperPortfolio(paperTrades, marketData, [], []);
      }
    }

    // ── Autotrader autonomous panel + pause/resume (owner-only) ──
    if (isAuthenticated) {
      try {
        const [agentControl, agentTrades, agentSnapshot, agentNav] = await Promise.all([
          fetchAgentControl(sb), fetchAgentTrades(sb), fetchAgentSnapshot(sb), fetchAgentNavSeries(sb),
        ]);
        renderAutotrader(sb, agentControl, agentTrades, agentSnapshot, agentNav, spyPrices, marketData);
      } catch (err) {
        console.error('[Autotrader] render error:', err);
      }
    }

    // ── Tuning strip: the headline dials, alongside the performance panels (owner-only) ──
    if (isAuthenticated) {
      try {
        await loadTuning(sb);
        renderTuningStrip();
      } catch (err) {
        console.error('[Tuning] load error:', err.message || err);
      }
    }

    // ── Playbook contextual-layer proposals (owner-only) ──
    if (isAuthenticated) {
      try {
        renderPlaybookPanel(await fetchContextualInputs(sb));
      } catch (err) {
        console.error('[Playbook] render error:', err);
      }
    }

    // ── On Radar (exploration candidates — public) ──
    try {
      const explorationCandidates = await fetchExplorationCandidates(sb);
      renderOnRadar(explorationCandidates, sb);
    } catch (err) {
      console.error('[OnRadar] render error:', err);
    }

    // ── Fetch convictions → build dynamic theme map ──
    const { data: convData } = await sb.from('convictions')
      .select('content').order('id', { ascending: false }).limit(1).single();
    if (convData?.content) {
      THEME_MAP = buildThemeMapFromConvictions(convData.content);
      _convictions = convData.content;
    }

    // Cache for re-renders triggered by conviction saves
    _lastMarketData        = marketData;
    _lastSignalsByTicker   = signalsByTicker;
    _lastDecisionsByTicker = decisionsByTicker;
    _lastPaperByTicker     = paperTradesByTicker;

    // ── Swim lanes ──
    renderLanes(marketData, signalsByTicker, decisionsByTicker, paperTradesByTicker);

    // Show content
    document.getElementById('loading-state').classList.add('hidden');
    document.getElementById('content').classList.remove('hidden');

  } catch (err) {
    console.error('ASTRA load error:', err);
    showError(err.message || 'Failed to connect to ASTRA database.');
  }
}

init();

/* ============================================================
   TUNING — the dials that govern what ASTRA buys, how big, and when it sells.
   Owner-only. Three layers resolve a value: code default → environment → this drawer.

   Two surfaces:
     • a live strip on the page, so the headline dials are visible while judging performance
     • a searchable drawer for changing them

   Unlike the convictions and playbook drawers, this one does NOT autosave. These values move
   real money, so edits stage into a draft and commit through an explicit review-then-save.
   ============================================================ */

let _tuneSchema  = null;   // settings_schema.json — labels, help, ranges (code defaults)
let _tuneSaved   = {};     // the override map currently persisted
let _tuneEffective = null; // what the last PRODUCTION run resolved to (includes the env layer)
let _tuneEffectiveAt = null;
let _tuneEffectiveSource = null;
let _tuneDraft   = {};     // staged, uncommitted edits
let _tuneRowId   = null;
let _tuneAdvanced = false;
let _tuneQuery   = '';
let _tuneConfirming = false;  // stage 2 of the commit flow
let _tuneReason  = '';        // required rationale, recorded with every change
let _tuneTyped   = '';        // typed confirmation for high-impact changes
let _tuneHistory = [];        // recent committed changes, with their reasons
let _tuneOpenGroups = null;   // Set of expanded group names — survives re-render

// The handful surfaced on the page itself, in the order they answer "what is ASTRA doing?"
const TUNING_HEADLINE = [
  'agent.trading_enabled',
  'brain.cp_buy_threshold',
  'brain.f_global',
  'brain.size_timing_max',
  'agent.reserve_floor_pct',
  'agent.max_deploy_per_run_pct',
  'brain.trail_atr_mult',
  'brain.contextual_enabled',
];

async function loadTuning(sb) {
  if (!_tuneSchema) {
    const res = await fetch('settings_schema.json', { cache: 'no-cache' });
    if (!res.ok) throw new Error(`settings schema ${res.status}`);
    _tuneSchema = await res.json();
  }
  const { data, error } = await sb.from('settings')
    .select('id, content').order('updated_at', { ascending: false }).limit(1);
  if (error) throw error;
  const row = (data || [])[0];
  _tuneRowId       = row?.id ?? null;
  _tuneSaved       = row?.content?.values ?? {};
  _tuneEffective   = row?.content?.effective ?? null;
  _tuneEffectiveAt = row?.content?.effective_at ?? null;
  _tuneEffectiveSource = row?.content?.effective_source ?? null;
  _tuneDraft = {};
  await loadTuningHistory();
}

function tuneKnob(key) {
  return (_tuneSchema?.settings || []).find(s => s.key === key);
}

// Value precedence for display: staged edit → saved override → what the last run resolved
// to (this is the only layer that knows about environment variables) → code default.
function tuneValue(key) {
  if (key in _tuneDraft) return _tuneDraft[key];
  if (key in _tuneSaved) return _tuneSaved[key];
  if (_tuneEffective && key in _tuneEffective) return _tuneEffective[key];
  return tuneKnob(key)?.default;
}
// The value that would apply with NO dashboard override — i.e. what the environment layer
// resolved to, falling back to the code default when no run has reported. This is the value
// an override must be compared against, NOT the code default: production sets
// AGENT_TRADING_ENABLED=true while the code default is false, so treating "equals the code
// default" as "no override needed" would silently drop the off-switch and leave trading on.
function tuneBaseValue(key) {
  if (_tuneEffective && key in _tuneEffective) return _tuneEffective[key];
  return tuneKnob(key)?.default;
}
function tuneSavedValue(key) {
  if (key in _tuneSaved) return _tuneSaved[key];
  return tuneBaseValue(key);
}
// Where the displayed value came from. 'unreported' matters: until a production run has
// written its effective snapshot we genuinely do not know what the environment layer
// resolved to, and showing the code default as though it were live would be a lie — the
// Autotrader switch is off in a local .env but on in production.
function tuneSource(key) {
  if (key in _tuneSaved) return 'dashboard';
  const k = tuneKnob(key);
  if (!_tuneEffective || !(key in _tuneEffective)) return 'unreported';
  if (k && !tuneValuesEqual(_tuneEffective[key], k.default)) return 'environment';
  return 'default';
}
// Reasons are free text that gets interpolated into markup (including into a <textarea>,
// where an unescaped closing tag would break the DOM out from under the commit flow).
function tuneEsc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
function tuneValuesEqual(a, b) {
  if (typeof a === 'number' && typeof b === 'number') return Math.abs(a - b) < 1e-9;
  return a === b;
}
function tuneIsDirty(key) {
  return key in _tuneDraft && !tuneValuesEqual(_tuneDraft[key], tuneSavedValue(key));
}
function tuneDirtyKeys() {
  return Object.keys(_tuneDraft).filter(tuneIsDirty);
}

function tuneFormat(value, knob) {
  if (typeof value === 'boolean') return value ? 'ON' : 'OFF';
  if (typeof value !== 'number') return String(value);
  switch (knob?.unit) {
    case 'fraction': return `${(value * 100).toFixed(value * 100 % 1 ? 1 : 0)}%`;
    case 'pct':      return `${value}%`;
    case 'usd':      return `$${value.toLocaleString()}`;
    case 'multiple': return `${value}×`;
    case 'atr':      return `${value}×`;
    case 'days':     return `${value}d`;
    case 'count':    return String(value);
    default:         return String(value);
  }
}

// ── Live strip ────────────────────────────────────────────────
function renderTuningStrip() {
  const section = document.getElementById('tuning-strip');
  if (!section || !_tuneSchema) return;
  const changed = Object.keys(_tuneSaved).length;

  const stamp = _tuneEffectiveAt
    ? `values as used by the ${_tuneEffectiveSource || 'scheduled'} run on ${new Date(_tuneEffectiveAt).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}`
    : 'no run has reported its settings yet — live values unknown';

  const chips = TUNING_HEADLINE.map(key => {
    const knob = tuneKnob(key);
    if (!knob) return '';
    const source = tuneSource(key);
    const value = tuneValue(key);
    // Never assert a value production has not reported — say so instead of guessing.
    if (source === 'unreported') {
      return `<div class="tuning-chip unreported">
          <div class="tuning-chip-label">${knob.label}</div>
          <div class="tuning-chip-value tuning-chip-unknown">—</div>
          <div class="tuning-chip-sub">awaiting a run</div>
        </div>`;
    }
    const valueHtml = (typeof value === 'boolean')
      ? `<div class="tuning-chip-value ${value ? 'tuning-chip-on' : 'tuning-chip-off'}">${value ? 'ON' : 'OFF'}</div>`
      : `<div class="tuning-chip-value">${tuneFormat(value, knob)}</div>`;
    return `<div class="tuning-chip ${source !== 'default' ? 'changed' : ''}">
        <div class="tuning-chip-label">${knob.label}</div>
        ${valueHtml}
        <div class="tuning-chip-sub">${source}</div>
      </div>`;
  }).join('');

  section.innerHTML = `
    <div class="section-label">◐ TUNING
      <span style="color:var(--text-dim);letter-spacing:0.5px;font-size:9px;margin-left:8px;">${stamp}</span>
    </div>
    <div class="card">
      <div class="tuning-strip-grid">${chips}</div>
      <div style="display:flex;align-items:center;gap:10px;margin-top:12px;">
        <span class="tuning-intro" style="margin:0;flex:1;">
          ${changed ? `${changed} setting${changed === 1 ? '' : 's'} overridden from this dashboard.` :
                      'No dashboard overrides — ASTRA is running on its code and environment settings.'}
        </span>
        <button class="tuning-adv-toggle" id="tuning-strip-open">ADJUST →</button>
      </div>
    </div>`;
  section.classList.remove('hidden');
  document.getElementById('tuning-strip-open')?.addEventListener('click', openTuningDrawer);

  const badge = document.getElementById('tuning-badge');
  if (badge) {
    badge.textContent = String(changed);
    badge.classList.toggle('hidden', changed === 0);
  }
}

// ── Drawer ────────────────────────────────────────────────────
function openTuningDrawer() {
  document.getElementById('tuning-backdrop').classList.remove('hidden');
  document.getElementById('tuning-drawer').classList.remove('hidden');
  document.body.style.overflow = 'hidden';
  renderTuningDrawer();
  setTimeout(() => document.getElementById('tuning-search')?.focus(), 60);
}

function closeTuningDrawer() {
  if (tuneDirtyKeys().length &&
      !confirm('You have unsaved changes. Close and discard them?')) return;
  _tuneDraft = {};
  _tuneReason = '';
  _tuneTyped = '';
  _tuneConfirming = false;
  document.getElementById('tuning-backdrop').classList.add('hidden');
  document.getElementById('tuning-drawer').classList.add('hidden');
  document.body.style.overflow = '';
  hidePbTooltip();
}

function tuneMatches(knob, q) {
  if (!q) return true;
  return `${knob.label} ${knob.help} ${knob.impact} ${knob.key} ${knob.group}`
    .toLowerCase().includes(q);
}

function renderTuningDrawer() {
  const body = document.getElementById('tuning-body');
  if (!body || !_tuneSchema) return;
  // Editing a value re-renders the whole list, so expanded groups and scroll position have
  // to survive it — otherwise changing one dial collapses its section and jumps to the top.
  const scrollTop = body.scrollTop;
  body.innerHTML = '';

  const q = _tuneQuery.trim().toLowerCase();
  const visible = _tuneSchema.settings.filter(s =>
    s.tier !== 3 && (_tuneAdvanced || s.tier === 1) && tuneMatches(s, q));

  const intro = document.createElement('div');
  intro.className = 'tuning-intro';
  intro.innerHTML =
    'Three layers decide a value: the built-in default, then an environment variable, then this drawer — ' +
    'last one wins. Every number is clamped to its safe range when the run starts. ' +
    'On/off switches can only ever be turned <b>off</b> here: enabling real trading still needs the environment secret. ' +
    '🔒 rows are hard risk limits — visible, but only changeable in code.';
  body.appendChild(intro);

  if (!visible.length) {
    const empty = document.createElement('div');
    empty.className = 'tuning-empty';
    empty.textContent = q
      ? `Nothing matches “${_tuneQuery}”.${_tuneAdvanced ? '' : ' Try ADVANCED for the scoring parameters.'}`
      : 'No settings to show.';
    body.appendChild(empty);
    return;
  }

  const groups = [];
  for (const name of _tuneSchema.groups) {
    const items = visible.filter(s => s.group === name);
    if (items.length) groups.push([name, items]);
  }

  // First open: expand the first group. After that, honour whatever the user has toggled.
  if (_tuneOpenGroups === null) _tuneOpenGroups = new Set(groups.length ? [groups[0][0]] : []);

  groups.forEach(([name, items]) => {
    const changedCount = items.filter(s => {
      const src = tuneSource(s.key);
      return src === 'dashboard' || src === 'environment';
    }).length;
    const wrap = document.createElement('div');
    // A search narrows to matches, so expand everything while a query is active.
    wrap.className = 'tuning-group' + (q || _tuneOpenGroups.has(name) ? ' open' : '');
    wrap.innerHTML = `
      <div class="tuning-group-head">
        <span class="tuning-group-chevron">▶</span>
        <span class="tuning-group-name">${name}</span>
        ${changedCount ? `<span class="tuning-group-changed">${changedCount} CHANGED</span>` : ''}
        <span class="tuning-group-count">${items.length}</span>
      </div>
      <div class="tuning-group-body"></div>`;
    wrap.querySelector('.tuning-group-head').addEventListener('click', () => {
      const open = wrap.classList.toggle('open');
      open ? _tuneOpenGroups.add(name) : _tuneOpenGroups.delete(name);
    });
    const gbody = wrap.querySelector('.tuning-group-body');
    items.forEach(knob => gbody.appendChild(buildTuningRow(knob)));
    body.appendChild(wrap);
  });

  if (!q) renderTuningHistory(body);
  renderTuningFooter();
  body.scrollTop = scrollTop;
}

function buildTuningRow(knob) {
  const row = document.createElement('div');
  const value = tuneValue(knob.key);
  const source = tuneSource(knob.key);
  row.className = 'tuning-row' +
    (tuneIsDirty(knob.key) ? ' dirty' : (source !== 'default' ? ' changed' : ''));

  const head = document.createElement('div');
  head.className = 'tuning-row-head';
  head.innerHTML = `
    <span class="tuning-label">
      ${knob.label}
      ${knob.locked ? '<span class="tuning-lock" title="Hard risk limit — change it in code">🔒</span>' : ''}
      <button class="tuning-info" aria-label="What this does" tabindex="0">ⓘ</button>
    </span>
    <span class="tuning-controls"></span>`;
  const controls = head.querySelector('.tuning-controls');

  const info = head.querySelector('.tuning-info');
  info.dataset.title = knob.label;
  info.dataset.help = knob.help;
  info.dataset.impact = knob.impact;
  info.dataset.range = (knob.min !== null && knob.min !== undefined)
    ? `${knob.min} – ${knob.max}` : '';
  info.dataset.def = tuneFormat(knob.default, knob);
  info.addEventListener('mouseenter', () => showTuningTooltip(info));
  info.addEventListener('focus', () => showTuningTooltip(info));
  info.addEventListener('mouseleave', hidePbTooltip);
  info.addEventListener('blur', hidePbTooltip);

  const disabled = !knob.editable;
  const onChange = v => {
    _tuneDraft[knob.key] = v;
    if (tuneValuesEqual(v, tuneSavedValue(knob.key))) delete _tuneDraft[knob.key];
    renderTuningDrawer();
  };

  if (knob.type === 'bool') {
    const label = document.createElement('label');
    label.className = 'pb-enable-toggle';
    label.title = disabled ? 'Locked' : 'Toggle';
    label.innerHTML = `<input type="checkbox" ${value ? 'checked' : ''} ${disabled ? 'disabled' : ''}>
                       <span class="pb-enable-slider"></span>`;
    label.querySelector('input').addEventListener('change', e => onChange(e.target.checked));
    controls.appendChild(label);
  } else if (knob.type === 'str') {
    const input = document.createElement('input');
    input.className = 'tuning-text';
    input.value = value ?? '';
    input.disabled = disabled;
    input.addEventListener('change', e => onChange(e.target.value));
    controls.appendChild(input);
  } else {
    const input = document.createElement('input');
    input.className = 'tuning-num';
    input.type = 'number';
    input.value = value;
    input.step = knob.step ?? 'any';
    if (knob.min !== null && knob.min !== undefined) input.min = knob.min;
    if (knob.max !== null && knob.max !== undefined) input.max = knob.max;
    input.disabled = disabled;
    input.addEventListener('change', e => {
      const v = parseFloat(e.target.value);
      if (!Number.isNaN(v)) onChange(knob.type === 'int' ? Math.round(v) : v);
    });
    controls.appendChild(input);
  }

  if (tuneIsDirty(knob.key) || (knob.key in _tuneSaved)) {
    const revert = document.createElement('button');
    revert.className = 'tuning-revert';
    revert.textContent = '⟲';
    revert.title = tuneIsDirty(knob.key)
      ? 'Undo this edit'
      : 'Remove this override — back to the value the run would use on its own';
    revert.addEventListener('click', () => {
      // Reverting means "stop overriding", so the target is the base value, not the code
      // default — those differ wherever the environment sets something.
      if (tuneIsDirty(knob.key)) delete _tuneDraft[knob.key];
      else _tuneDraft[knob.key] = tuneBaseValue(knob.key);
      renderTuningDrawer();
    });
    controls.appendChild(revert);
  }

  row.appendChild(head);

  // Slider for bounded numbers — a dial you can feel, with the number for precision.
  if (!disabled && knob.type !== 'bool' && knob.type !== 'str' &&
      knob.min !== null && knob.min !== undefined && knob.max !== null) {
    const sr = document.createElement('div');
    sr.className = 'tuning-slider-row';
    sr.innerHTML = `<span class="tuning-bound">${knob.min}</span>
                    <input type="range" class="tuning-slider" min="${knob.min}" max="${knob.max}"
                           step="${knob.step ?? 'any'}" value="${value}">
                    <span class="tuning-bound max">${knob.max}</span>`;
    const slider = sr.querySelector('.tuning-slider');
    slider.addEventListener('input', e => {
      const v = parseFloat(e.target.value);
      head.querySelector('.tuning-num').value = v;
    });
    slider.addEventListener('change', e => {
      const v = parseFloat(e.target.value);
      onChange(knob.type === 'int' ? Math.round(v) : v);
    });
    row.appendChild(sr);
  }

  const meta = document.createElement('div');
  meta.className = 'tuning-meta';
  const parts = [
    source === 'unreported'
      ? `<span class="tuning-src tuning-src-unreported" title="No production run has reported this value yet — the figure shown is the code default, which the environment may override">not yet reported by a run</span>`
      : `<span class="tuning-src tuning-src-${source}">${source}</span>`,
  ];
  if (!tuneValuesEqual(value, knob.default)) {
    parts.push(`default ${tuneFormat(knob.default, knob)}`);
  }
  if (tuneIsDirty(knob.key)) {
    parts.push(`<span style="color:var(--accent-cyan)">unsaved: ${tuneFormat(tuneSavedValue(knob.key), knob)} → ${tuneFormat(value, knob)}</span>`);
  }
  meta.innerHTML = parts.join('<span style="color:var(--text-dim)">·</span>');
  row.appendChild(meta);

  return row;
}

function showTuningTooltip(icon) {
  const tip = ensurePbTooltip();
  const d = icon.dataset;
  tip.innerHTML =
    `<div class="pb-tip-title">${d.title}</div>
     <div class="pb-tip-desc">${d.help}</div>
     <div class="pb-tip-desc" style="margin-top:8px;color:var(--text-muted);"><b>Effect:</b> ${d.impact}</div>` +
    (d.range ? `<div class="pb-tip-range"><span>RANGE</span> ${d.range} <span class="pb-tip-def">· default ${d.def}</span></div>` : '');
  tip.classList.add('tuning-tip');
  tip.classList.remove('hidden');
  positionPbTip(tip, icon);
}

// ── Review → confirm → commit ─────────────────────────────────
// Three deliberate steps before a dial moves. These values steer real money, so committing
// is not a single click: you review a diff, write down why, and press-and-hold to commit.
// Changes with the widest blast radius (any on/off switch, anything in the Autotrader group)
// additionally require typing COMMIT.

const TUNING_HOLD_MS = 1400;
const TUNING_TYPED_WORD = 'COMMIT';

function tuneIsCritical(key) {
  const knob = tuneKnob(key);
  return !!knob && (knob.type === 'bool' || knob.group === 'Autotrader (real money)');
}

function tuneDiffRows(keys) {
  return keys.map(key => {
    const knob = tuneKnob(key);
    const crit = tuneIsCritical(key);
    return `<div class="tuning-diff-row">
        <span class="tuning-diff-name ${crit ? 'tuning-diff-critical' : ''}">${knob.label}</span>
        <span>${tuneFormat(tuneSavedValue(key), knob)}</span>
        <span class="tuning-diff-arrow">→</span>
        <span class="tuning-diff-to">${tuneFormat(_tuneDraft[key], knob)}</span>
      </div>`;
  }).join('');
}

function renderTuningFooter() {
  const footer = document.getElementById('tuning-footer');
  if (!footer) return;
  const dirty = tuneDirtyKeys();
  if (!dirty.length) {
    footer.classList.add('hidden');
    footer.innerHTML = '';
    _tuneConfirming = false;
    return;
  }
  footer.classList.remove('hidden');
  _tuneConfirming ? renderTuningConfirm(footer, dirty) : renderTuningReview(footer, dirty);
}

// Stage 1 — what you changed.
function renderTuningReview(footer, dirty) {
  footer.innerHTML = `
    <div class="tuning-diff">${tuneDiffRows(dirty)}</div>
    <div class="tuning-footer-actions">
      <span class="tuning-footer-count">${dirty.length} unsaved change${dirty.length === 1 ? '' : 's'}</span>
      <button class="tuning-discard" id="tuning-discard">DISCARD</button>
      <button class="tuning-save" id="tuning-review">REVIEW &amp; COMMIT →</button>
    </div>`;
  footer.querySelector('#tuning-discard').addEventListener('click', () => {
    _tuneDraft = {};
    renderTuningDrawer();
  });
  footer.querySelector('#tuning-review').addEventListener('click', () => {
    _tuneConfirming = true;
    renderTuningFooter();
    setTimeout(() => footer.querySelector('.tuning-reason')?.focus(), 50);
  });
}

// Stage 2 — why, and a hold to commit.
function renderTuningConfirm(footer, dirty) {
  const critical = dirty.filter(tuneIsCritical);
  footer.innerHTML = `
    <div class="tuning-confirm-title">
      Commit ${dirty.length} change${dirty.length === 1 ? '' : 's'} — applies on the next run
      ${critical.length ? `<span class="tuning-critical-badge">${critical.length} HIGH IMPACT</span>` : ''}
    </div>
    <div class="tuning-diff">${tuneDiffRows(dirty)}</div>
    <div class="tuning-reason-label">
      Reason for this change <span class="tuning-reason-req">required</span>
    </div>
    <textarea class="tuning-reason" id="tuning-reason" rows="2" spellcheck="true"
      placeholder="What made you change this? e.g. “buying too eagerly into weak setups — raising the bar for a few weeks to see if fewer, stronger entries do better”">${tuneEsc(_tuneReason)}</textarea>
    ${critical.length ? `
      <div class="tuning-reason-label">
        Type <b style="color:var(--accent-red)">${TUNING_TYPED_WORD}</b> to confirm the high-impact change
      </div>
      <input class="tuning-typed" id="tuning-typed" spellcheck="false" autocomplete="off"
             placeholder="${TUNING_TYPED_WORD}" value="${tuneEsc(_tuneTyped)}">` : ''}
    <div class="tuning-footer-actions">
      <button class="tuning-discard" id="tuning-back">← BACK</button>
      <button class="tuning-hold" id="tuning-hold">
        <span class="tuning-hold-fill"></span>
        <span class="tuning-hold-label">HOLD TO COMMIT</span>
      </button>
    </div>`;

  const reason = footer.querySelector('#tuning-reason');
  const typed = footer.querySelector('#tuning-typed');
  const hold = footer.querySelector('#tuning-hold');

  const ready = () => (_tuneReason || '').trim().length >= 8 &&
    (!critical.length || (_tuneTyped || '').trim().toUpperCase() === TUNING_TYPED_WORD);
  const sync = () => { hold.disabled = !ready(); };

  reason.addEventListener('input', e => { _tuneReason = e.target.value; sync(); });
  typed?.addEventListener('input', e => { _tuneTyped = e.target.value; sync(); });
  footer.querySelector('#tuning-back').addEventListener('click', () => {
    _tuneConfirming = false;
    renderTuningFooter();
  });
  sync();
  attachHoldToCommit(hold, () => saveTuning(_tuneReason.trim(), dirty));
}

// Press-and-hold with a progress fill. Releasing early cancels — the friction is the point.
// Keyboard-accessible: hold Enter or Space.
function attachHoldToCommit(btn, onComplete) {
  const fill = btn.querySelector('.tuning-hold-fill');
  const label = btn.querySelector('.tuning-hold-label');
  let raf = null, timer = null, start = 0, done = false;

  const stop = () => {
    if (raf) cancelAnimationFrame(raf);
    if (timer) clearTimeout(timer);
    raf = timer = null;
    if (done) return;
    fill.style.width = '0%';
    btn.classList.remove('armed');
    label.textContent = 'HOLD TO COMMIT';
  };
  // The commit fires off a plain timer so it never depends on animation frames, which the
  // browser freezes in a backgrounded tab. requestAnimationFrame only paints the fill.
  const paint = () => {
    const pct = Math.min(100, ((performance.now() - start) / TUNING_HOLD_MS) * 100);
    fill.style.width = `${pct}%`;
    if (pct < 100) raf = requestAnimationFrame(paint);
  };
  const complete = () => {
    if (done) return;
    done = true;
    if (raf) cancelAnimationFrame(raf);
    fill.style.width = '100%';
    label.textContent = 'COMMITTING…';
    onComplete();
  };
  const begin = e => {
    if (btn.disabled || done) return;
    e.preventDefault();
    start = performance.now();
    btn.classList.add('armed');
    label.textContent = 'KEEP HOLDING…';
    timer = setTimeout(complete, TUNING_HOLD_MS);
    raf = requestAnimationFrame(paint);
  };

  btn.addEventListener('pointerdown', begin);
  btn.addEventListener('pointerup', stop);
  btn.addEventListener('pointerleave', stop);
  btn.addEventListener('pointercancel', stop);
  btn.addEventListener('keydown', e => {
    if ((e.key === 'Enter' || e.key === ' ') && !e.repeat) begin(e);
  });
  btn.addEventListener('keyup', stop);
  btn.addEventListener('blur', stop);
}

function setTuningSaveStatus(state, msg) {
  const el = document.getElementById('tuning-save-status');
  if (!el) return;
  el.className = 'conv-save-status ' + (state || '');
  el.textContent = msg || '';
}

async function saveTuning(reason, dirty) {
  dirty = dirty || tuneDirtyKeys();
  if (!dirty.length || !_sb || !reason) return;
  setTuningSaveStatus('saving', '● Committing…');

  // The structured diff recorded alongside the reason, so history is queryable without
  // diffing successive blobs.
  const changed = dirty.map(key => ({
    key, label: tuneKnob(key).label, from: tuneSavedValue(key), to: _tuneDraft[key],
  }));

  // Merge the staged edits into the saved map. An override is dropped only when it matches
  // what would apply WITHOUT it (the environment layer, or the code default when unreported)
  // — dropping on "equals the code default" instead would delete a deliberate off-switch on
  // any setting the environment turns on, silently reinstating it on the next run.
  const values = { ...(_tuneSaved || {}) };
  for (const key of dirty) {
    if (tuneValuesEqual(_tuneDraft[key], tuneBaseValue(key))) delete values[key];
    else values[key] = _tuneDraft[key];
  }

  try {
    // Read-modify-write: the backend writes the `effective` key of this same row each run,
    // so the whole content object must be preserved, not replaced.
    const { data: cur } = await _sb.from('settings')
      .select('id, content').order('updated_at', { ascending: false }).limit(1);
    const existing = (cur || [])[0];
    const content = { ...(existing?.content || {}), values };

    if (existing) {
      const { error } = await _sb.from('settings')
        .update({ content, updated_at: new Date().toISOString(), updated_by: 'dashboard' })
        .eq('id', existing.id);
      if (error) throw error;
      _tuneRowId = existing.id;
    } else {
      const { data: ins, error } = await _sb.from('settings')
        .insert({ content, updated_at: new Date().toISOString(), updated_by: 'dashboard' })
        .select('id').single();
      if (error) throw error;
      _tuneRowId = ins?.id ?? null;
    }

    // The reason + structured diff are the point of the history table — they answer "why did
    // this dial move" long after the fact, alongside the performance it produced.
    const { error: histErr } = await _sb.from('settings_history')
      .insert({ content, reason, changed, saved_at: new Date().toISOString() });
    if (histErr) console.error('settings_history append failed:', histErr.message);

    logUserAction('settings_edit', null, { reason, changed });

    _tuneSaved = values;
    _tuneDraft = {};
    _tuneReason = '';
    _tuneTyped = '';
    _tuneConfirming = false;
    setTuningSaveStatus('saved', '● Committed — applies on the next run');
    setTimeout(() => setTuningSaveStatus('', ''), 4000);
    await loadTuningHistory();
    renderTuningDrawer();
    renderTuningStrip();
  } catch (err) {
    setTuningSaveStatus('error', '● Commit failed');
    console.error('Tuning save error:', err.message || err);
    _tuneConfirming = false;
    renderTuningFooter();
  }
}

// ── Change history ────────────────────────────────────────────
async function loadTuningHistory() {
  if (!_sb) return;
  try {
    const { data, error } = await _sb.from('settings_history')
      .select('saved_at, reason, changed').order('saved_at', { ascending: false }).limit(12);
    if (error) throw error;
    _tuneHistory = (data || []).filter(r => r.reason || (r.changed || []).length);
  } catch (err) {
    console.error('[Tuning] history load failed:', err.message || err);
    _tuneHistory = [];
  }
}

function renderTuningHistory(container) {
  if (!_tuneHistory.length) return;
  const wrap = document.createElement('div');
  wrap.className = 'tuning-history';
  wrap.innerHTML = `<div class="conv-section-label">CHANGE HISTORY</div>` +
    _tuneHistory.map(entry => {
      const when = new Date(entry.saved_at).toLocaleString('en-US',
        { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
      const changes = (entry.changed || []).map(c => {
        const knob = tuneKnob(c.key);
        return `<div>${tuneEsc(c.label || c.key)}
          <span style="color:var(--text-dim)">${tuneFormat(c.from, knob)} →</span>
          <span class="tuning-hist-to">${tuneFormat(c.to, knob)}</span></div>`;
      }).join('');
      return `<div class="tuning-hist-entry">
          <div class="tuning-hist-when">${when}</div>
          ${entry.reason ? `<div class="tuning-hist-reason">“${tuneEsc(entry.reason)}”</div>` : ''}
          <div class="tuning-hist-changes">${changes}</div>
        </div>`;
    }).join('');
  container.appendChild(wrap);
}

function initTuningDrawer() {
  document.getElementById('tuning-btn').addEventListener('click', openTuningDrawer);
  document.getElementById('tuning-close').addEventListener('click', closeTuningDrawer);
  document.getElementById('tuning-backdrop').addEventListener('click', closeTuningDrawer);
  document.getElementById('tuning-body').addEventListener('scroll', hidePbTooltip);
  document.getElementById('tuning-search').addEventListener('input', e => {
    _tuneQuery = e.target.value;
    renderTuningDrawer();
  });
  document.getElementById('tuning-adv-toggle').addEventListener('click', e => {
    _tuneAdvanced = !_tuneAdvanced;
    e.target.classList.toggle('active', _tuneAdvanced);
    renderTuningDrawer();
  });
  document.addEventListener('keydown', e => {
    const open = !document.getElementById('tuning-drawer').classList.contains('hidden');
    if (e.key === 'Escape' && open) closeTuningDrawer();
    // ⌘K / Ctrl-K jumps straight to searching the dials.
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k' &&
        !document.getElementById('tuning-btn').classList.contains('hidden')) {
      e.preventDefault();
      if (open) document.getElementById('tuning-search').focus();
      else openTuningDrawer();
    }
  });
}
