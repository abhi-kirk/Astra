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

// Theme → tickers map (drives swim lanes)
const THEME_MAP = {
  space:     { label: 'Space',     conviction: 'very_high', tickers: ['RKLB','ASTS','ARKX','SPCX','NASA','SPCE','SMR'] },
  core_tech: { label: 'Core Tech', conviction: 'high',      tickers: ['NVDA','GOOGL','AMZN','AAPL','MSFT','AMD','CRM','NFLX','CRSR','BB'] },
  ev:        { label: 'EV',        conviction: 'high',      tickers: ['NIO','BYDDY','LCID','CHPT'] },
  cannabis:  { label: 'Cannabis',  conviction: 'low',       tickers: ['CRON','SNDL','VFF'] },
  other:     { label: 'Other',     conviction: null,         tickers: [] }, // catch-all
};

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

function nextMonday6amPT() {
  const now = new Date();
  // Monday = 1 in getDay(). PT is UTC-7 (PDT) or UTC-8 (PST).
  // We'll just compute next Monday's date in local time for simplicity.
  const day = now.getDay(); // 0=Sun, 1=Mon,...
  const daysUntilMonday = day === 1 ? 7 : (1 - day + 7) % 7 || 7;
  const nextMon = new Date(now);
  nextMon.setDate(now.getDate() + daysUntilMonday);
  nextMon.setHours(6, 0, 0, 0);
  return nextMon;
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

// ── Signals donut chart ──────────────────────────────────────

let donutChart = null;

function renderSignalsDonut(counts) {
  const ctx = document.getElementById('signals-donut').getContext('2d');

  const labels = ['Buy', 'Watch', 'Review', 'Hold', 'Blocked'];
  const data   = [counts.buy, counts.watch, counts.review, counts.hold, counts.blocked];
  const colors = [
    'rgba(57,211,83,0.85)',   // buy → green
    'rgba(0,212,255,0.75)',   // watch → cyan
    'rgba(255,215,0,0.75)',   // review → yellow
    'rgba(68,85,102,0.5)',    // hold → muted
    'rgba(68,85,102,0.3)',    // blocked → dim
  ];

  if (donutChart) donutChart.destroy();

  donutChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{
        data,
        backgroundColor: colors,
        borderColor: 'rgba(5,10,20,0.8)',
        borderWidth: 2,
      }],
    },
    options: {
      cutout: '70%',
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => ` ${ctx.label}: ${ctx.parsed}`,
          },
          backgroundColor: 'rgba(13,30,53,0.95)',
          borderColor: 'rgba(26,58,92,1)',
          borderWidth: 1,
          titleColor: '#8899aa',
          bodyColor: '#e8f4fd',
          titleFont: { family: 'JetBrains Mono', size: 10 },
          bodyFont:  { family: 'JetBrains Mono', size: 12 },
        },
      },
      animation: { duration: 800 },
    },
  });
}

// ── Position card ────────────────────────────────────────────

function buildCard(ticker, mdata, signal, decision) {
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

  // For actionable signals: show top 2 reasons why ASTRA flagged this
  let bodyHtml = '';
  if (['buy', 'watch', 'review'].includes(action) && signal?.reasons?.length) {
    const topReasons = signal.reasons.slice(0, 2);
    const icons = { buy: '▲', watch: '◈', review: '◉' };
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
    <div class="card-name">${shortName}</div>
    <div class="card-price-row">
      <span class="card-price">${fmtPrice(price)}</span>
      ${pnlHtml}
    </div>
    ${bodyHtml}
  `;

  card.addEventListener('click', () => openModal(ticker, mdata, signal, decision));
  return card;
}

// ── Swim lanes ───────────────────────────────────────────────

function renderLanes(marketData, signalsByTicker, decisionsByTicker) {
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
    const ORDER = { buy: 0, review: 1, watch: 2, hold: 3, blocked: 4 };
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
      );
      grid.appendChild(card);
    }

    container.appendChild(lane);
  }
}

// ── Modal ────────────────────────────────────────────────────

let modalBarChart = null;

function openModal(ticker, mdata, signal, decision) {
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
  const verdictItems = [
    {
      label: 'Conviction', cls: signal?.conviction_match ? 'pass' : signal ? 'fail' : 'na',
      value: signal?.conviction_match ? '✓ MATCH' : signal ? '✗ NO MATCH' : '—',
      tip: 'Does this ticker appear in your approved conviction themes? E.g. space, core tech, EV.',
    },
    {
      label: 'Quality', cls: signal?.quality_pass ? 'pass' : signal ? 'fail' : 'na',
      value: signal?.quality_pass ? '✓ PASS' : signal ? '✗ FAIL' : '—',
      tip: 'Passes ASTRA quality filter: revenue growth >10% YoY, gross margin >30%, manageable debt.',
    },
    {
      label: 'Technical', cls: signal?.technical_pass ? 'pass' : signal ? 'warn' : 'na',
      value: signal?.technical_pass ? '✓ ENTRY' : signal ? '✗ NOT YET' : '—',
      tip: 'Technical entry signal: price >15% below 52-week high AND RSI below 40 (oversold dip).',
    },
    {
      label: 'Hard Rules', cls: signal?.hard_rule_block ? 'fail' : signal ? 'pass' : 'na',
      value: signal?.hard_rule_block ? '✗ BLOCKED' : signal ? '✓ CLEAR' : '—',
      tip: 'Hard constraint check: not TSLA, not averaging down past 3x on positions >35% below cost, not a "hold only" ticker.',
    },
  ];

  const verdictHtml = verdictItems.map(v => `
    <div class="verdict-item" title="${v.tip}">
      <div class="verdict-label">${v.label}</div>
      <div class="verdict-value ${v.cls}">${v.value}</div>
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
  `;

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

// ── Main render ──────────────────────────────────────────────

function showError(msg) {
  document.getElementById('loading-state').classList.add('hidden');
  const errEl = document.getElementById('error-state');
  errEl.classList.remove('hidden');
  errEl.querySelector('.error-text').textContent = '⚠ ' + msg;
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

  // Check existing session (Supabase auto-restores from localStorage JWT)
  const { data: { session } } = await sb.auth.getSession();
  isAuthenticated = !!session;
  updateLockBtn(isAuthenticated);
  initLockButton(sb);

  try {
    let raw, decisions = [], runDate;

    if (isAuthenticated) {
      // Full data fetch — includes raw_output, advisor_note, decisions
      const runRow = await fetchLatestRunSummary(sb);
      const rawFull = runRow.raw_output || {};
      raw = Array.isArray(rawFull) ? { signals: rawFull } : rawFull;
      runDate = runRow.run_date;
      decisions = await fetchLatestDecisions(sb, runDate);
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
    document.getElementById('last-run-date').textContent =
      runDateObj.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });

    const nextMon = nextMonday6amPT();
    document.getElementById('next-run-date').textContent =
      nextMon.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ' 6am PT';

    document.getElementById('positions-count').textContent =
      raw.num_positions_screened ?? Object.keys(marketData).length;

    // ── Signals counts ──
    const counts = { buy: 0, watch: 0, review: 0, hold: 0, blocked: 0 };
    const totalPositions = Object.keys(marketData).length;
    signals.forEach(s => { if (counts[s.action] !== undefined) counts[s.action]++; });
    counts.hold = totalPositions - counts.buy - counts.watch - counts.review - counts.blocked;

    renderSignalsDonut(counts);

    const countsEl = document.getElementById('signals-counts');
    countsEl.innerHTML = [
      ['buy', 'BUY'],
      ['watch', 'WATCH'],
      ['review', 'REVIEW'],
      ['hold', 'HOLD'],
    ].map(([action, label]) => `
      <div class="signal-count-item ${action}">
        <span class="signal-count-num">${counts[action]}</span>
        <span class="signal-count-label">${label}</span>
      </div>
    `).join('');

    // ── Structured signals summary ──
    const summaryEl = document.getElementById('run-summary-text');
    const byAction = {};
    signals.forEach(s => { (byAction[s.action] = byAction[s.action] || []).push(s.ticker); });
    const summaryRows = [
      { action: 'buy',    label: '↑ BUY',    tickers: byAction.buy    || [] },
      { action: 'review', label: '◉ REVIEW', tickers: byAction.review || [] },
      { action: 'watch',  label: '◈ WATCH',  tickers: byAction.watch  || [] },
    ].filter(r => r.tickers.length);

    if (summaryRows.length) {
      summaryEl.innerHTML = `<div class="summary-structured">${
        summaryRows.map(r => `
          <div class="summary-row">
            <span class="summary-action-label ${r.action}">${r.label}</span>
            <span class="summary-tickers">${r.tickers.map(t =>
              `<span class="ticker-link-plain" data-ticker="${t}">${t}</span>`
            ).join('<span style="color:var(--text-dim)"> ·</span> ')}</span>
          </div>`).join('')
      }</div>`;
    } else {
      summaryEl.textContent = raw.summary ?? '';
    }

    // ── Advisor note ──
    const noteEl = document.getElementById('advisor-note');
    const noteText = raw.advisor_note ?? '';
    const allTickers = Object.keys(marketData);

    if (!isAuthenticated) {
      noteEl.innerHTML = `<div class="advisor-locked">
        <div class="advisor-locked-icon">🔒</div>
        <div>ASTRA's weekly analysis contains personal financial data.</div>
        <button class="advisor-locked-btn" id="advisor-unlock-btn">SIGN IN TO VIEW</button>
      </div>`;
      document.getElementById('advisor-unlock-btn')?.addEventListener('click', showLoginModal);
    } else if (noteText && window.marked) {
      const rendered = linkifyText(DOMPurify.sanitize(marked.parse(noteText)), allTickers);
      // Split after the first section (Priority Actions = second heading block).
      // Claude uses ## headings — find index of each <h tag, split at the second one.
      // Structure: h1 title → h2 PRIORITY ACTIONS → h2 RISK FLAGS → ...
      // Always show title + Priority Actions; collapse everything from RISK FLAGS onward.
      // That means split at the 3rd heading (index 2). Fall back to 2nd if only 2 exist.
      const headingRe = /<h[1-6][\s>]/gi;
      let hMatch, hPositions = [];
      while ((hMatch = headingRe.exec(rendered)) !== null) hPositions.push(hMatch.index);
      const splitIdx = hPositions.length >= 3 ? hPositions[2]
                     : hPositions.length >= 2 ? hPositions[1]
                     : -1;
      if (splitIdx > -1) {
        const always      = rendered.slice(0, splitIdx);
        const collapsible = rendered.slice(splitIdx);
        noteEl.innerHTML = `
          ${always}
          <button class="advisor-collapse-btn" id="advisor-toggle">
            <span>Show full analysis</span>
            <span class="advisor-collapse-arrow">▼</span>
          </button>
          <div class="advisor-collapsible" id="advisor-collapsible">
            ${collapsible}
          </div>`;
        const btn  = document.getElementById('advisor-toggle');
        const body = document.getElementById('advisor-collapsible');
        btn.addEventListener('click', () => {
          const open = body.classList.toggle('open');
          btn.classList.toggle('open', open);
          btn.querySelector('span').textContent = open ? 'Hide full analysis' : 'Show full analysis';
        });
      } else {
        noteEl.innerHTML = DOMPurify.sanitize(rendered);
      }
    } else if (noteText) {
      noteEl.textContent = noteText;
    } else if (isAuthenticated) {
      noteEl.innerHTML = `<p class="advisor-no-note">
        No advisor note for this run — the agent was run without the AI reasoning step.<br><br>
        To generate one: <code>PYTHONPATH=. .venv/bin/python -m src.agent --mode simulation</code>
      </p>`;
    }

    // ── Swim lanes ──
    renderLanes(marketData, signalsByTicker, decisionsByTicker);

    // Show content
    document.getElementById('loading-state').classList.add('hidden');
    document.getElementById('content').classList.remove('hidden');

  } catch (err) {
    console.error('ASTRA load error:', err);
    showError(err.message || 'Failed to connect to ASTRA database.');
  }
}

init();
