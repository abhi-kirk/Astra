-- Migration 022: scorecard_runs — ASTRA's own read-only verdict on its track record.
-- Phase 3 is "scale the sleeve as the track record justifies", but until now nothing read the
-- NAV series back and formed a judgment: performance.py computes the curve, the dashboard draws
-- it, and that was the end of the loop. src/scorecard.py appends one row here per track per run.
--
-- Two axes are stored SEPARATELY and never blended into one number (docs/attribution.md's
-- "segment, never pool" rule): execution quality (NAV vs a cash-drag-adjusted benchmark, from
-- the paired daily series) and pick quality (decision-level forward returns vs the same
-- benchmark over the same window). Crossing them separates "the timing/sizing side is leaking
-- alpha" (tunable) from "the convictions are wrong" (a human question), and the readiness
-- counts explain a hold instead of guessing.
--
-- Read-only w.r.t. the decision path: the scorecard writes this table and nothing else, and
-- nothing else writes this table. Owner-only, mirroring agent_account_snapshots/agent_runs —
-- a funding recommendation for the real-money sleeve plus a self-critique is private capital
-- posture, not part of the public paper-track story that migration 014 exposed to anon.
-- Apply once in: Supabase Dashboard → SQL Editor.

CREATE TABLE IF NOT EXISTS public.scorecard_runs (
    id                      BIGSERIAL PRIMARY KEY,
    scored_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    scored_date             DATE        NOT NULL,      -- run day this verdict was formed on
    as_of_date              DATE,                      -- last PAIRED date the metrics cover
    track                   TEXT        NOT NULL,      -- paper | autotrader

    verdict                 TEXT        NOT NULL,      -- INSUFFICIENT_DATA|STEADY|SCALE|TUNE|REVIEW
    prev_verdict            TEXT,                      -- previous row's verdict for this track
    notified                BOOLEAN     DEFAULT FALSE, -- did this row fire a Telegram alert

    -- ── readiness gate — ALL must clear before a decisive verdict is allowed ──
    ready                   BOOLEAN     NOT NULL,
    nav_days                INTEGER,                   -- retained paired return observations
    nav_span_days           INTEGER,                   -- calendar span of the paired window
    dropped_gap_obs         INTEGER,                   -- observations discarded for a long gap
    closed_trips            INTEGER,                   -- full closes only (a trim is not a trip)
    matured_decisions       INTEGER,                   -- outcome rows passing the horizon filter
    dropped_horizon         INTEGER,                   -- outcome rows whose realized horizon failed
    distinct_tickers        INTEGER,                   -- effective sample size for the pick axis
    distinct_decision_weeks INTEGER,                   -- calendar diversity of the graded calls
    missing                 JSONB,                     -- ["nav days 13/90", "finished trades 8/20"]
    eta_days                INTEGER,                   -- projected days until the gate clears

    -- ── axis 1: execution quality (NAV vs benchmark) ─────────────────────────
    exec_axis               TEXT,                      -- good | bad | flat
    exec_cum_excess_raw     NUMERIC(14,8),             -- vs fully-invested SPY (matches the chart)
    exec_cum_excess_adj     NUMERIC(14,8),             -- vs invested-fraction-scaled SPY (primary)
    exec_mean_excess        NUMERIC(16,10),            -- per observation
    exec_stdev_excess       NUMERIC(16,10),
    exec_ir_annual          NUMERIC(14,8),             -- reported; never the sole gate at small n
    exec_periods_per_year   NUMERIC(10,4),             -- from observed cadence, not a fixed 252
    exec_ci_low             NUMERIC(16,10),            -- block-bootstrap interval on cum excess
    exec_ci_high            NUMERIC(16,10),
    exec_sign_wins          INTEGER,                   -- robust cross-check: positive intervals
    exec_sign_n             INTEGER,
    exec_sign_p             NUMERIC(12,8),             -- exact binomial, two-sided
    exec_mean_invested_frac NUMERIC(10,6),             -- cash-drag context for the TUNE diagnostic
    bench_source            TEXT,                      -- yfinance | benchmark_prices

    -- ── axis 2: pick quality (decision forward returns vs benchmark) ─────────
    pick_axis               TEXT,                      -- good | bad | flat
    pick_window_days        INTEGER,                   -- horizon evaluated (default 30)
    pick_hit_rate           NUMERIC(10,6),             -- share beating SPY over the same window
    pick_hit_rate_p         NUMERIC(12,8),             -- exact binomial, two-sided
    pick_mean_excess        NUMERIC(14,8),             -- row-level mean (correlated; context only)
    pick_clustered_excess   NUMERIC(14,8),             -- ticker-clustered mean (the honest number)
    pick_ci_low             NUMERIC(14,8),
    pick_ci_high            NUMERIC(14,8),

    -- ── narrative + audit ────────────────────────────────────────────────────
    diagnostic              JSONB,                     -- TUNE fingerprints; never applied settings
    reasons                 JSONB,                     -- why this verdict, human-readable lines
    metrics                 JSONB,                     -- full result blob for later re-analysis
    brain_code_version      TEXT,                      -- which brain produced the graded record
    brain_config_hash       TEXT,
    created_at              TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_scorecard_runs_track_time
    ON public.scorecard_runs(track, scored_at DESC);
CREATE INDEX IF NOT EXISTS idx_scorecard_runs_verdict
    ON public.scorecard_runs(verdict);

-- Deliberately NO unique constraint on (track, scored_date): a manual re-run appending a second
-- row is harmless (the verdict is unchanged, so no alert re-fires) and preserves a genuine audit
-- trail of what ASTRA concluded at each point in the day.

-- GRANT matrix (see docs/architecture.md): backend writes via service_role; owner reads.
GRANT SELECT ON public.scorecard_runs TO authenticated;
GRANT ALL    ON public.scorecard_runs TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.scorecard_runs_id_seq TO service_role;

ALTER TABLE public.scorecard_runs ENABLE ROW LEVEL SECURITY;
CREATE POLICY "owner read scorecard_runs" ON public.scorecard_runs
    FOR SELECT TO authenticated
    USING (auth.email() = 'abhikirk@icloud.com');
