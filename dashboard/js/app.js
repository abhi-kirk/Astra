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
  const el = e.target.closest('.ticker-link, .ticker-link-plain');
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
  return data || { paused: false, halted: false, halt_reason: null, baseline_equity: null };
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

function renderAutotrader(sb, control, trades, snapshot) {
  const section = document.getElementById("autotrader");
  if (!section) return;
  section.classList.remove('hidden');

  const halted = !!control.halted;
  const paused = !!control.paused;
  const statusLabel = halted ? 'HALTED' : (paused ? 'PAUSED' : 'ACTIVE');
  const statusColor = halted ? '#ff5c5c' : (paused ? '#e0a500' : '#38d977');

  const equity = snapshot?.total_equity;
  const drawdown = snapshot?.drawdown_pct;
  const ddColor = (drawdown != null && drawdown < 0) ? '#ff5c5c' : 'var(--text-dim)';

  const open = trades.filter(t => t.is_open);
  const money = v => (v == null ? '—' : `$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`);

  const rows = trades.slice(0, 25).map(t => {
    const sideColor = t.side === 'buy' ? '#38d977' : '#ff5c5c';
    const size = t.dollar_amount != null ? money(t.dollar_amount) : (t.quantity != null ? `${t.quantity} sh` : '—');
    return `<tr>
      <td>${(t.run_date || '').slice(0, 10)}</td>
      <td style="font-weight:600">${t.ticker}</td>
      <td style="color:${sideColor};font-weight:600">${(t.side || '').toUpperCase()}</td>
      <td>${size}</td>
      <td>${t.status || ''}${t.status === 'dry_run' ? ' <span style="color:var(--text-dim)">(sim)</span>' : ''}</td>
    </tr>`;
  }).join('');

  section.innerHTML = `
    <div class="paper-header" style="display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap">
      <div>
        <h2 style="margin:0">AUTOTRADER · AUTONOMOUS <span style="font-size:12px;color:var(--text-dim)">real-money agentic account</span></h2>
        <div style="margin-top:4px">
          <span style="color:${statusColor};font-weight:700;letter-spacing:.5px">● ${statusLabel}</span>
          ${halted && control.halt_reason ? `<span style="color:var(--text-dim);font-size:12px"> — ${control.halt_reason}</span>` : ''}
        </div>
      </div>
      <button id="autotrader-toggle" class="paper-tab" ${halted ? 'disabled title="Halted — reset in DB after review"' : ''}
        style="cursor:${halted ? 'not-allowed' : 'pointer'};min-width:120px">
        ${paused ? '▶ RESUME' : '⏸ PAUSE'}
      </button>
    </div>
    <div style="display:flex;gap:24px;flex-wrap:wrap;margin:14px 0">
      <div><div style="font-size:12px;color:var(--text-dim)">EQUITY</div><div style="font-size:20px;font-weight:700">${money(equity)}</div></div>
      <div><div style="font-size:12px;color:var(--text-dim)">DRAWDOWN</div><div style="font-size:20px;font-weight:700;color:${ddColor}">${drawdown != null ? drawdown.toFixed(1) + '%' : '—'}</div></div>
      <div><div style="font-size:12px;color:var(--text-dim)">OPEN POSITIONS</div><div style="font-size:20px;font-weight:700">${open.length}</div></div>
      <div><div style="font-size:12px;color:var(--text-dim)">ORDERS LOGGED</div><div style="font-size:20px;font-weight:700">${trades.length}</div></div>
    </div>
    ${trades.length ? `<div style="overflow-x:auto"><table class="paper-table" style="width:100%">
      <thead><tr><th>DATE</th><th>TICKER</th><th>SIDE</th><th>SIZE</th><th>STATUS</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`
      : '<div style="color:var(--text-dim);padding:12px 0">No agentic orders yet.</div>'}
  `;

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
      renderAutotrader(sb, { ...control, paused: next }, trades, snapshot);
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

function renderPaperPortfolio(paperTrades, marketData) {
  const section = document.getElementById('paper-portfolio');
  if (!paperTrades.length) { section.classList.add('hidden'); return; }
  section.classList.remove('hidden');

  let totalCost = 0, totalValue = 0;
  const rows = paperTrades.map(pt => {
    const mdata = marketData[pt.ticker] || {};
    const cur   = mdata.current_price ?? pt.price_at_signal;
    const pnlD  = (cur - pt.price_at_signal) * pt.virtual_shares;
    const pnlPct = (cur - pt.price_at_signal) / pt.price_at_signal * 100;
    totalCost  += pt.virtual_cost;
    totalValue += cur * pt.virtual_shares;

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
          <div class="paper-stat-value paper-stat-na" title="Need 30+ days of data for benchmark comparison">— <span class="paper-stat-note">&lt;30d</span></div>
        </div>
      </div>
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
    </div>`;

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
  initJournalDrawer();

  // Auth-gated header buttons
  if (isAuthenticated) {
    document.getElementById('convictions-btn').classList.remove('hidden');
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

    // Always render all three action rows (even when empty) so BUY/SELL/WATCH
    // labels stay vertically aligned; empty rows show a "—" placeholder.
    const summaryRows = [
      { action: 'buy',   label: '↑ BUY',   sigs: byAction.buy   || [] },
      { action: 'sell',  label: '↓ SELL',  sigs: byAction.sell  || [] },
      { action: 'watch', label: '◈ WATCH', sigs: byAction.watch || [] },
    ];

    summaryEl.innerHTML = `<div class="summary-structured">${
      summaryRows.map(r => `
        <div class="summary-row">
          <span class="summary-action-label ${r.action}">${r.label}</span>
          <span class="summary-tickers">${r.sigs.length
            ? r.sigs.map(s =>
                `<span class="summary-ticker-unit${r.action === 'buy' && buyThrottled(s) ? ' buy-throttled' : ''}"><span class="ticker-link-plain" data-ticker="${s.ticker}">${s.ticker}</span>${signalBadge(r.action, s)}</span>`
              ).join('<span class="summary-sep"> · </span>')
            : '<span class="summary-empty">—</span>'}</span>
        </div>`).join('')
    }</div>`;

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
    if (isAuthenticated) renderPaperPortfolio(paperTrades, marketData);

    // ── Autotrader autonomous panel + pause/resume (owner-only) ──
    if (isAuthenticated) {
      try {
        const [agentControl, agentTrades, agentSnapshot] = await Promise.all([
          fetchAgentControl(sb), fetchAgentTrades(sb), fetchAgentSnapshot(sb),
        ]);
        renderAutotrader(sb, agentControl, agentTrades, agentSnapshot);
      } catch (err) {
        console.error('[Autotrader] render error:', err);
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
