-- =====================================================================
-- Migration: forecast_bid_adjustments
-- Date:      2026-04-28
--
-- Purpose:   Stores pre-computed bid-strategy adjustment factors
--            per (week_start, business). These factors capture the
--            combined effect of tROAS and eLTV changes on expected
--            lead volume, using the delta method (comparing future-
--            week ratios to trailing 4-week ratios).
--
--            Populated by analyze_troas_adjustment.py (run locally,
--            since it depends on Google Sheets + Daily ROAS.xlsx).
--            Consumed by push_to_sheets.py (run in CI) to apply
--            adjustments to the baseline forecast before pushing
--            to Google Sheets.
--
-- Grain:     One row per (week_start, business).
--
-- Key column: bid_delta
--   = 1.0 + 0.5 * (bid_delta_raw - 1.0)
--   The 50% weight aligns with the Supabase baseline's 50/50 blend
--   of YoY and WoW components. The delta method only measures
--   divergence from the YoY half, so we halve its impact.
--
--   >1.0 = future week is MORE aggressive than trailing => more leads
--   <1.0 = future week is LESS aggressive than trailing => fewer leads
--    1.0 = no change needed (baseline already reflects current regime)
--
-- This migration is NOT idempotent. Do not re-run without reviewing.
-- =====================================================================

BEGIN;

CREATE TABLE forecast_bid_adjustments (
    -- ── GRAIN ────────────────────────────────────────────────────────
    week_start          DATE        NOT NULL,
    -- Sunday of the week (aligns with leads_forecast.week_start).

    business            TEXT        NOT NULL,
    -- 'VT Core' | 'International' | 'Prof Certs'

    -- ── ADJUSTMENT FACTORS ──────────────────────────────────────────
    bid_delta           NUMERIC     NOT NULL DEFAULT 1.0,
    -- Final weighted adjustment factor applied to forecast.
    -- 1.0 + 0.5 * (bid_delta_raw - 1.0)

    bid_delta_raw       NUMERIC,
    -- Pre-weighting delta: troas_delta * eltv_delta

    -- ── COMPONENT RATIOS ────────────────────────────────────────────
    troas_ratio         NUMERIC,
    -- PY_tROAS / CY_planned_tROAS (absolute, before delta)

    eltv_ratio          NUMERIC,
    -- CY_eLTV / PY_eLTV (absolute, before delta)

    troas_delta         NUMERIC,
    -- troas_ratio / trailing_troas_ratio

    eltv_delta          NUMERIC,
    -- eltv_ratio / trailing_eltv_ratio

    -- ── INPUTS (for auditability) ───────────────────────────────────
    planned_troas       NUMERIC,
    py_troas            NUMERIC,
    cy_eltv             NUMERIC,
    py_eltv             NUMERIC,

    -- ── TRAILING ANCHORS ────────────────────────────────────────────
    trail_troas_ratio   NUMERIC,
    trail_eltv_ratio    NUMERIC,

    -- ── METADATA ────────────────────────────────────────────────────
    source              TEXT,
    -- Provenance: 'PY 2025-04-20', 'interpolated', etc.

    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- ── CONSTRAINTS ─────────────────────────────────────────────────
    PRIMARY KEY (week_start, business)
);

-- ── INDEXES ──────────────────────────────────────────────────────────

CREATE INDEX forecast_bid_adjustments_week_idx
    ON forecast_bid_adjustments (week_start);

-- ── COLUMN COMMENTS ──────────────────────────────────────────────────

COMMENT ON TABLE forecast_bid_adjustments IS
    'Pre-computed bid-strategy adjustment factors (delta method). Populated locally by analyze_troas_adjustment.py, consumed by push_to_sheets.py in CI.';

COMMENT ON COLUMN forecast_bid_adjustments.bid_delta IS
    'Final adjustment factor: 1.0 + 0.5*(raw-1). Applied as: leads_impact = baseline * paid_mix * (bid_delta - 1).';
COMMENT ON COLUMN forecast_bid_adjustments.bid_delta_raw IS
    'Pre-weighting: troas_delta * eltv_delta. Represents full YoY divergence before 50% blend dampening.';
COMMENT ON COLUMN forecast_bid_adjustments.troas_delta IS
    'This week tROAS ratio / trailing 4-wk tROAS ratio. >1 = more aggressive than recent.';
COMMENT ON COLUMN forecast_bid_adjustments.eltv_delta IS
    'This week eLTV ratio / trailing 4-wk eLTV ratio. >1 = higher eLTV than recent.';
COMMENT ON COLUMN forecast_bid_adjustments.source IS
    'Provenance: PY date used, interpolated, nearest, etc.';

COMMIT;
