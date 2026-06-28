# ASTRA — AI Trading Advisor + Bot

<p align="center">
  <img src="dashboard/img/astra-logo.jpeg" alt="ASTRA" width="120"/>
</p>

**ASTRA** (AI-powered Stock Trading & Reasoning Agent) is a conviction-based trading advisor that progressively earns autonomy through demonstrated performance. It does not replace human judgment — it systematizes it.

The core idea: most investors have informed sector theses but inconsistent execution. ASTRA acts as a disciplined co-pilot that screens positions daily, surfaces entry signals, reasons over live market data, and builds a paper trading track record before ever touching real capital. Autonomy is earned through results, not granted upfront.

<p align="center">
  <img src="docs/astra-dashboard.png" alt="ASTRA Mission Control Dashboard" width="900"/>
</p>

**Live dashboard:** [abhi-kirk.github.io/Astra](https://abhi-kirk.github.io/Astra/)

---

## How ASTRA Works

ASTRA runs a daily pipeline (weekdays, 6am PT via GitHub Actions) with three stages:

```
1. DATA PULL       yfinance EOD prices, RSI, 52w high/low, fundamentals
                        ↓
2. STRATEGY SCREEN  Quality filter + technical signal + hard rule enforcement
                        ↓
3. AI REASONING    Claude (claude-sonnet-4-6) + Tavily news search
                   → structured signal per ticker + narrative advisor note
                        ↓
4. LOG + TRACK     Decisions → Supabase · Paper trades opened/closed
                        ↓
5. DASHBOARD       GitHub Pages — public signals + auth-gated P&L
```

Each signal is one of: **BUY** · **SELL** · **WATCH** · **HOLD** · **BLOCKED**

BUY signals automatically open a paper trade (fractional virtual position). Paper trades close when the signal changes — building a live track record before any real capital is deployed.

---

## Strategy

ASTRA's screening framework has three layers:

### 1 — Conviction gate
Tickers must belong to an approved conviction theme (defined in `convictions.json`). Positions marked `hold_only` or `do_not_add` are blocked from new buy signals regardless of technicals.

### 2 — Quality filter
- Revenue growth > 10% YoY
- Gross margin > 30% (or improving)
- Manageable debt/equity
- Positive free cash flow

At least 2 of 4 must pass with no catastrophic flags (negative margins, declining revenue).

### 3 — Technical entry signal
- Price > 15% below 52-week high (dip)
- RSI < 40 (oversold)

Both required for a BUY. One alone → WATCH.

### Hard rules (non-negotiable)
- No averaging down past 3 buys on positions > 35% below average cost
- No single name > 10% of portfolio
- No conviction theme > 15% of portfolio
- Profit-take review triggered at > 60% unrealized gain

---

## Architecture

| Layer | Tech |
|---|---|
| Language | Python 3.12 |
| Market data | yfinance (EOD — price, RSI, fundamentals) |
| Database | Supabase (Postgres) — decisions, paper trades, run summaries |
| AI reasoning | Anthropic API · `claude-sonnet-4-6` · Mustache prompt templates |
| News enrichment | Tavily MCP (web search per ticker, per run) |
| Automation | GitHub Actions (daily weekday runs) |
| Dashboard | GitHub Pages — public signals + auth-gated P&L |
| Config | `starlette.Config` + `.env` |
| Prompt templating | `chevron` (Mustache) |
| Parallel data fetch | `concurrent.futures.ThreadPoolExecutor` |

### Three interaction modes

1. **Conviction Update** — edit `convictions.json` to define themes, tickers, and hold/buy status
2. **Daily Analysis Run** — automated GitHub Actions pipeline; outputs to Supabase and dashboard
3. **Trade Review** — walk through recommendations, discuss, approve or reject before any execution

### Dashboard privacy tiers

- **Public**: tickers, signals, signal reasons (scrubbed of cost basis / P&L)
- **Authenticated**: full advisor note, avg cost, unrealized P&L, suggested position size, paper trade performance

RLS is enforced server-side — private data never reaches the browser for unauthenticated visitors.

---

## Roadmap

| Phase | Goal | Status |
|---|---|---|
| **1** | Simulation — data pipeline, strategy engine, AI reasoning, Supabase logging, dashboard | Complete ✅ |
| **1.5** | Daily runs, Tavily news MCP, paper trading (auto open/close), signal history | In progress 🔄 |
| **2** | Real money · human approval for every BUY and SELL · small initial budget | Planned |
| **3** | Expanded limits · partial automation for high-conviction clear-signal trades | Future |
| **4** | Full automation within defined guardrails (conviction themes, position caps, per-trade limits) | Future |

Autonomy expands only as the paper trading track record justifies it.

---

## Project Structure

```
trading/
├── src/
│   ├── config.py          # Centralized config via starlette.Config
│   ├── db.py              # Supabase client singleton
│   ├── data_layer.py      # yfinance market data (parallel fetch)
│   ├── strategy.py        # Screening rules, signal generation, hard rule enforcement
│   ├── memory.py          # Decision log, run summaries, paper trades
│   └── agent.py           # Orchestrator: pipeline → Claude API + MCP → Supabase
├── prompts/
│   └── advisor_note.mustache   # Prompt template for AI reasoning
├── supabase/
│   ├── schema.sql         # Full schema + RLS setup
│   └── seed.py            # One-time seed script
├── dashboard/             # GitHub Pages static site
│   ├── index.html
│   ├── css/style.css
│   └── js/app.js
└── .github/workflows/
    ├── daily_analysis.yml  # Weekday 6am PT pipeline run
    └── deploy_pages.yml    # Auto-deploy dashboard on push to main
```

---

## Running Locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Copy and fill in secrets
cp .env.template .env

# Full run with AI reasoning
python -m src.agent

# Skip Claude API (mechanical signals only)
python -m src.agent --no-ai

# Single ticker
python -m src.agent --ticker RKLB
```

Required environment variables (see `.env.template`):

```
SUPABASE_URL
SUPABASE_SERVICE_KEY
ANTHROPIC_API_KEY
TAVILY_MCP_URL        # optional — enables news enrichment
```
