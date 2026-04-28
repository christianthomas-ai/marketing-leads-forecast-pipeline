-- =====================================================================
-- Migration: marketing_eltv_actuals
-- Date:      2026-04-24
--
-- Purpose:   Weekly eLTV (estimated Lifetime Value) per client, by BU.
--            Fed by a Looker webhook delivering client_count and
--            conv_value; eLTV/client is derived (conv_value / client_count).
--
--            Used by the forecast adjustment pipeline to normalize
--            PY-vs-CY tROAS comparisons. The effective bid per lead is
--            eLTV / tROAS -- without both, the tROAS comparison is
--            incomplete (a tROAS of 2.75 at $1,600 eLTV is a very
--            different bid than 2.75 at $2,100 eLTV).
--
-- Ingest:    Looker webhook (JSON) -> Supabase Edge Function
--            -> INSERT into this table.
--            Initial backfill from a static CSV (Apr 2024 - Apr 2026).
--
-- Grain:     One row per (week_start, business).
--            VT Core and International currently share the same bidding
--            portfolio (identical eLTV values), stored as separate rows
--            to support future portfolio splits.
--
-- This migration is NOT idempotent. Do not re-run without reviewing.
-- =====================================================================

BEGIN;

CREATE TABLE marketing_eltv_actuals (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- ── GRAIN ────────────────────────────────────────────────────────
    week_start          DATE        NOT NULL,
    -- Sunday of the week (aligns with leads_forecast.week_start).

    business            TEXT        NOT NULL,
    -- 'VT Core' | 'International' | 'Prof Certs'
    -- Same vocabulary as leads_forecast.Business.

    -- ── RAW VALUES (from Looker) ─────────────────────────────────────
    client_count        INTEGER,
    -- Number of conversions reported to Google in this week.

    conv_value          NUMERIC,
    -- Total conversion value reported to Google (client_count x eLTV).

    -- ── DERIVED ──────────────────────────────────────────────────────
    eltv_per_client     NUMERIC GENERATED ALWAYS AS (
                            CASE WHEN client_count > 0
                                 THEN ROUND(conv_value / client_count, 2)
                                 ELSE NULL
                            END
                        ) STORED,
    -- Computed column: conv_value / client_count.
    -- Using a generated column so the value is always consistent
    -- with the raw inputs, regardless of ingest path.

    -- ── AUDIT ────────────────────────────────────────────────────────
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    source              TEXT
    -- 'looker:marketing_contribution_conversion_feed'
    -- 'backfill:static_csv_2026-04-24'
);

-- ── INDEXES ──────────────────────────────────────────────────────────

-- Primary lookup: PY same-week comparison.
CREATE UNIQUE INDEX marketing_eltv_actuals_week_bu_uq
    ON marketing_eltv_actuals (week_start, business);

-- Time-range scans for trailing averages.
CREATE INDEX marketing_eltv_actuals_week_idx
    ON marketing_eltv_actuals (week_start);

-- ── COLUMN COMMENTS ──────────────────────────────────────────────────

COMMENT ON TABLE marketing_eltv_actuals IS
    'Weekly eLTV per client by BU, from the Google conversion feed via Looker. Used to normalize tROAS comparisons across eLTV regimes.';

COMMENT ON COLUMN marketing_eltv_actuals.week_start IS
    'Sunday of the reporting week. Aligns with leads_forecast.week_start.';
COMMENT ON COLUMN marketing_eltv_actuals.client_count IS
    'Number of conversions reported to Google this week.';
COMMENT ON COLUMN marketing_eltv_actuals.conv_value IS
    'Total conversion value reported to Google (client_count x eLTV sent to Google).';
COMMENT ON COLUMN marketing_eltv_actuals.eltv_per_client IS
    'Derived: conv_value / client_count. The per-conversion value Google uses for Smart Bidding.';
COMMENT ON COLUMN marketing_eltv_actuals.source IS
    'Provenance tag: looker:..., backfill:..., etc.';

COMMIT;
