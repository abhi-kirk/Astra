// ============================================================
// ASTRA — Dashboard App
// ============================================================

const SUPABASE_URL  = 'https://irnpxdnuldvawhbjscmc.supabase.co';
const SUPABASE_ANON = 'sb_publishable_sSt4OK7825qQ-DZqf8o_Ow_JGbV2G7G';

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

const ACTION_COLORS = {
  buy:     '#ff6b35',
  watch:   '#00d4ff',
  review:  '#ffd700',
  hold:    '#445566',
  blocked: '#445566',
};

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
  if (n >= 1e12) return '$' + (n / 1e12).toFixed(1) + 'T';
  if (n >= 1e9)  return '$' + (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6)  return '$' + (n / 1e6).toFixed(1) + 'M';
  return '$' + n.toFixed(0);
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
    .order('run_date', { ascending: false })
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
    'rgba(255,107,53,0.85)',
    'rgba(0,212,255,0.75)',
    'rgba(255,215,0,0.75)',
    'rgba(68,85,102,0.5)',
    'rgba(68,85,102,0.3)',
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

  const price = mdata?.current_price;
  const rsi   = mdata?.rsi_14;
  const pctBelow = mdata?.pct_below_52w_high;
  const avgCost  = decision?.avg_cost;

  let pnlHtml = '';
  if (avgCost && price) {
    const pnl = ((price - avgCost) / avgCost) * 100;
    const cls = pnl >= 0 ? 'positive' : 'negative';
    pnlHtml = `<span class="card-pnl ${cls}">${fmtPct(pnl)}</span>`;
  }

  const rsiPct = rsi != null ? Math.min(rsi, 100) : 0;
  const rsiFillClass = rsiClass(rsi);
  const pctBelowFill = pctBelow != null ? Math.min(pctBelow, 100) : 0;

  const badgeClass = `badge-${action}`;
  const badgeLabel = action.toUpperCase();

  const shortName = mdata?.short_name
    ? (mdata.short_name.length > 22 ? mdata.short_name.slice(0, 22) + '…' : mdata.short_name)
    : '';

  card.innerHTML = `
    <div class="card-header">
      <span class="card-ticker">${ticker}</span>
      <span class="signal-badge ${badgeClass}">${badgeLabel}</span>
    </div>
    <div class="card-name">${shortName}</div>
    <div class="card-price-row">
      <span class="card-price">${fmtPrice(price)}</span>
      ${pnlHtml}
    </div>
    <div class="card-metrics">
      <div class="metric-row">
        <span class="metric-label">RSI</span>
        <div class="metric-bar-track">
          <div class="metric-bar-fill rsi-fill ${rsiFillClass}" style="width:${rsiPct}%"></div>
        </div>
        <span class="metric-value">${rsi != null ? fmt(rsi, 0) : '—'}</span>
      </div>
      <div class="metric-row">
        <span class="metric-label">↓52W</span>
        <div class="metric-bar-track">
          <div class="metric-bar-fill" style="width:${pctBelowFill}%; background: var(--accent-cyan); opacity: 0.6;"></div>
        </div>
        <span class="metric-value">${pctBelow != null ? fmt(pctBelow, 0) + '%' : '—'}</span>
      </div>
    </div>
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
      ? `<span class="lane-conviction conviction-${cfg.conviction}">${cfg.conviction.replace('_', ' ')}</span>`
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

let modalRsiChart = null;
let modalMetricsChart = null;

function openModal(ticker, mdata, signal, decision) {
  const overlay = document.getElementById('modal-overlay');
  const content = document.getElementById('modal-content');

  overlay.classList.remove('hidden');
  document.body.style.overflow = 'hidden';

  if (modalRsiChart)     { modalRsiChart.destroy();     modalRsiChart = null; }
  if (modalMetricsChart) { modalMetricsChart.destroy(); modalMetricsChart = null; }

  const action   = signal?.action ?? 'hold';
  const price    = mdata?.current_price;
  const avgCost  = decision?.avg_cost;
  const rsi      = mdata?.rsi_14;
  const pctBelow = mdata?.pct_below_52w_high;
  const revGrowth = mdata?.revenue_growth_yoy != null ? mdata.revenue_growth_yoy * 100 : null;
  const grossMgn  = mdata?.gross_margins       != null ? mdata.gross_margins       * 100 : null;

  let pnlStr = '—', pnlClass = '';
  if (avgCost && price) {
    const pnl = ((price - avgCost) / avgCost) * 100;
    pnlStr = fmtPct(pnl);
    pnlClass = pnl >= 0 ? 'positive' : 'negative';
  }

  const badgeClass = `badge-${action}`;
  const badgeLabel = action.toUpperCase();

  // Metric cards
  const metrics = [
    { label: 'Current Price', value: fmtPrice(price), cls: '', sub: avgCost ? `Avg cost ${fmtPrice(avgCost)}` : '' },
    { label: 'Unrealized P&L', value: pnlStr, cls: pnlClass, sub: '' },
    { label: 'RSI (14)', value: rsi != null ? fmt(rsi, 1) : '—', cls: rsiClass(rsi) === 'oversold' ? 'positive' : rsiClass(rsi) === 'overbought' ? 'negative' : '', sub: rsiLabel(rsi) },
    { label: '% Below 52W High', value: pctBelow != null ? fmt(pctBelow, 1) + '%' : '—', cls: pctBelow > 15 ? 'cyan' : '', sub: mdata?.high_52w ? `Peak ${fmtPrice(mdata.high_52w)}` : '' },
    { label: 'Revenue Growth', value: revGrowth != null ? fmtPct(revGrowth) : '—', cls: revGrowth > 10 ? 'positive' : revGrowth != null ? 'warning' : '', sub: 'YoY' },
    { label: 'Gross Margin', value: grossMgn != null ? fmt(grossMgn, 1) + '%' : '—', cls: grossMgn > 30 ? 'positive' : grossMgn != null ? 'warning' : '', sub: 'Trailing' },
  ];

  const metricsHtml = metrics.map(m => `
    <div class="modal-metric-card">
      <div class="modal-metric-label">${m.label}</div>
      <div class="modal-metric-value ${m.cls}">${m.value}</div>
      ${m.sub ? `<div class="modal-metric-sub">${m.sub}</div>` : ''}
    </div>
  `).join('');

  // Reasons + risks
  const reasons = signal?.reasons ?? [];
  const risks   = signal?.risk_flags ?? [];

  const reasonsHtml = reasons.length
    ? reasons.map(r => `<div class="signal-reason-item">${r}</div>`).join('')
    : '<div class="no-data">No signals for this position</div>';

  const risksHtml = risks.length
    ? risks.map(r => `<div class="risk-item">${r}</div>`).join('')
    : '<div class="no-data">No risk flags</div>';

  const suggestedSize = signal?.suggested_position_pct != null
    ? `<div style="margin-top:12px; font-family:var(--font-mono); font-size:11px; color:var(--accent-cyan);">
        Suggested size: ${(signal.suggested_position_pct * 100).toFixed(0)}% of portfolio
      </div>`
    : '';

  // More fundamentals
  const fundamentals = [
    ['Market Cap',    fmtBigNum(mdata?.market_cap)],
    ['Fwd P/E',       fmt(mdata?.forward_pe, 1)],
    ['D/E Ratio',     fmt(mdata?.debt_to_equity, 1)],
    ['Current Ratio', fmt(mdata?.current_ratio, 2)],
    ['Free Cash Flow',fmtBigNum(mdata?.free_cashflow)],
    ['MA 50',         fmtPrice(mdata?.ma_50)],
    ['MA 200',        fmtPrice(mdata?.ma_200)],
    ['Vs MA50',       mdata?.price_vs_ma50_pct != null ? fmtPct(mdata.price_vs_ma50_pct) : '—'],
  ];

  const fundHtml = fundamentals.map(([l, v]) => `
    <div class="modal-metric-card" style="padding:10px 14px;">
      <div class="modal-metric-label">${l}</div>
      <div class="modal-metric-value" style="font-size:15px;">${v}</div>
    </div>
  `).join('');

  content.innerHTML = `
    <div class="modal-header">
      <div>
        <div class="modal-ticker">${ticker}</div>
        <div class="modal-name">${mdata?.short_name ?? ticker}</div>
        <div class="modal-sector">${mdata?.sector ?? ''} · ${mdata?.industry ?? ''}</div>
      </div>
      <div class="modal-badge-wrap">
        <span class="signal-badge ${badgeClass}" style="font-size:11px; padding:4px 12px;">${badgeLabel}</span>
      </div>
    </div>

    <div class="modal-metrics">${metricsHtml}</div>

    <div class="modal-chart-row">
      <div class="modal-chart-card">
        <div class="modal-chart-title">RSI (14) — Momentum</div>
        <div class="rsi-gauge-wrap">
          <div class="rsi-value-display">${rsi != null ? fmt(rsi, 1) : '—'}</div>
          <div style="color:${rsiLabelColor(rsi)}; font-family:var(--font-mono); font-size:10px; letter-spacing:2px; margin-bottom:10px;">
            ${rsiLabel(rsi)}
          </div>
          <div class="rsi-gauge">
            <div class="rsi-needle" style="left:${rsi != null ? rsi : 50}%"></div>
          </div>
          <div class="rsi-zones">
            <span>0 OVERSOLD</span>
            <span>40</span>
            <span>60</span>
            <span>OVERBOUGHT 100</span>
          </div>
        </div>
      </div>

      <div class="modal-chart-card">
        <div class="modal-chart-title">Key Screening Metrics</div>
        <canvas id="modal-bar-chart" height="140"></canvas>
      </div>
    </div>

    <div class="modal-two-col">
      <div class="modal-signal-block">
        <h4>Signal Reasons</h4>
        ${reasonsHtml}
        ${suggestedSize}
      </div>
      <div class="modal-signal-block">
        <h4>Risk Flags</h4>
        ${risksHtml}
      </div>
    </div>

    <div style="margin-top:20px;">
      <div class="modal-chart-title" style="font-family:var(--font-mono); font-size:9px; letter-spacing:2px; color:var(--text-muted); text-transform:uppercase; margin-bottom:12px;">Fundamentals</div>
      <div style="display:grid; grid-template-columns:repeat(4,1fr); gap:8px;">${fundHtml}</div>
    </div>
  `;

  // Bar chart: key metrics
  requestAnimationFrame(() => {
    const barCtx = document.getElementById('modal-bar-chart');
    if (!barCtx) return;

    const barLabels = ['RSI', '% Bel 52W', 'Rev Gr%', 'Gr Mgn%'];
    const barValues = [
      rsi ?? 0,
      pctBelow ?? 0,
      Math.max(0, Math.min(100, revGrowth ?? 0)),
      Math.max(0, Math.min(100, grossMgn ?? 0)),
    ];
    const barColors = [
      rsiClass(rsi) === 'oversold' ? 'rgba(57,211,83,0.7)' : rsiClass(rsi) === 'overbought' ? 'rgba(255,51,85,0.7)' : 'rgba(136,153,170,0.5)',
      pctBelow > 15 ? 'rgba(0,212,255,0.7)' : 'rgba(136,153,170,0.5)',
      (revGrowth ?? 0) > 10 ? 'rgba(57,211,83,0.7)' : 'rgba(255,215,0,0.6)',
      (grossMgn ?? 0) > 30  ? 'rgba(57,211,83,0.7)' : 'rgba(255,215,0,0.6)',
    ];

    modalMetricsChart = new Chart(barCtx, {
      type: 'bar',
      data: {
        labels: barLabels,
        datasets: [{
          data: barValues,
          backgroundColor: barColors,
          borderRadius: 4,
          borderSkipped: false,
        }],
      },
      options: {
        indexAxis: 'y',
        scales: {
          x: {
            min: 0,
            max: 100,
            grid: { color: 'rgba(26,58,92,0.4)' },
            ticks: { color: '#445566', font: { family: 'JetBrains Mono', size: 9 }, maxTicksLimit: 5 },
            border: { color: 'rgba(26,58,92,0.5)' },
          },
          y: {
            grid: { display: false },
            ticks: { color: '#8899aa', font: { family: 'JetBrains Mono', size: 10 } },
            border: { display: false },
          },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: { label: ctx => ' ' + ctx.parsed.x.toFixed(1) },
            backgroundColor: 'rgba(13,30,53,0.95)',
            borderColor: 'rgba(26,58,92,1)',
            borderWidth: 1,
            bodyColor: '#e8f4fd',
            bodyFont: { family: 'JetBrains Mono', size: 12 },
          },
        },
        animation: { duration: 600 },
      },
    });
  });
}

function closeModal() {
  document.getElementById('modal-overlay').classList.add('hidden');
  document.body.style.overflow = '';
  if (modalRsiChart)     { modalRsiChart.destroy();     modalRsiChart = null; }
  if (modalMetricsChart) { modalMetricsChart.destroy(); modalMetricsChart = null; }
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
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

  let sb;
  try {
    sb = createSupabaseClient();
  } catch (err) {
    showError(err.message);
    return;
  }

  try {
    const runRow = await fetchLatestRunSummary(sb);
    const raw    = runRow.raw_output || {};

    const decisions   = await fetchLatestDecisions(sb, runRow.run_date);
    const marketData  = raw.market_data_snapshot || {};
    const signals     = raw.signals || [];

    // Index by ticker
    const signalsByTicker   = Object.fromEntries(signals.map(s => [s.ticker, s]));
    const decisionsByTicker = Object.fromEntries(decisions.map(d => [d.ticker, d]));

    // ── Header ──
    const runDate = new Date(runRow.run_date);
    document.getElementById('last-run-date').textContent =
      runDate.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });

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

    document.getElementById('run-summary-text').textContent = raw.summary ?? '';

    // ── Advisor note ──
    const noteEl = document.getElementById('advisor-note');
    const noteText = raw.advisor_note ?? '';
    if (noteText && window.marked) {
      noteEl.innerHTML = marked.parse(noteText);
    } else {
      noteEl.textContent = noteText || 'No advisor note for this run.';
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
