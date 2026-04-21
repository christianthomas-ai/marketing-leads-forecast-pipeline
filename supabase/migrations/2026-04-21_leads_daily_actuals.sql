-- =====================================================================
-- Migration: leads_daily_actuals (initial)
-- Date:      2026-04-21
-- NOTE:      This table and its views were renamed to
--            marketing_leads_daily_actuals / marketing_leads_daily_actuals_current
--            / marketing_forecast_vs_actual_daily later on 2026-04-21 to
--            namespace marketing-owned objects. See
--            supabase/migrations/2026-04-21_rename_marketing_tables.sql.
--            This file is kept verbatim as history.
--
-- Purpose:   Per-day, per-BU, per-lead_source, per-audience actuals table
--            with paid/organic split. Companion to forecast_vintages for
--            forecast-vs-actual analysis at the plan's native grain.
--
-- Relationship to existing tables:
--   - leads_weekly_actuals (existing): weekly grain, populated by the
--     Looker -> Supabase Edge Function pipeline. Source of truth for
--     the SOP weekly summary view today. NOT removed by this migration.
--   - leads_daily_actuals (this):      daily grain, paid/organic split.
--     Needed to reconcile against forecast_vintages (which is daily)
--     without aggregation loss.
--
-- Ingest path is intentionally NOT coupled to this migration. See
-- supabase/migrations/README.md for the three candidate paths (new Looker
-- schedule / daily derivation from the existing weekly actuals /
-- direct API pull) and pick one during Phase 1 item 5 design.
--
-- This migration is NOT idempotent. Do not re-run without reviewing.
-- =====================================================================

BEGIN;

CREATE TABLE leads_daily_actuals (
    -- Surrogate primary key. Mirrors forecast_vintages so late-arriving
    -- corrections can insert new rows instead of requiring an UPDATE.
    id                  BIGSERIAL PRIMARY KEY,

    -- ────────────────────────────────────────────────────────────────
    -- GRAIN (matches forecast_vintages one-for-one, PLUS paid_channel)
    -- ────────────────────────────────────────────────────────────────
    actual_date         DATE        NOT NULL,
    -- The calendar day these actuals cover. Naming differs from
    -- forecast_vintages.forecast_date on purpose (forecast = prediction,
    -- actual = observed) to make mistaken joins obvious in code review.

    business            TEXT        NOT NULL,
    -- 'VT Core' | 'International' | 'Prof Certs' — same vocabulary as
    -- forecast_vintages.business. Keep these in lockstep.

    lead_source         TEXT        NOT NULL,
    -- Follows the Sheet tab convention (NOT Looker raw or Model mapping
    -- variants). Same vocabulary as forecast_vintages.lead_source.

    audience            TEXT        NOT NULL,
    -- Same vocabulary as forecast_vintages.audience.

    paid_channel        TEXT        NOT NULL,
    -- 'paid' | 'organic'. This is the dimension forecast_vintages does
    -- NOT have today — the marketing plan is all-in on spend and leads,
    -- but actuals need the split to measure paid media efficiency. If
    -- the plan starts splitting, add paid_channel to forecast_vintages
    -- as a separate migration and relax this rule at that time.
    --
    -- Kept as TEXT (not an ENUM) so we can add values cheaply
    -- ('partner', 'affiliate', etc.) without an ALTER TYPE ritual.

    -- ────────────────────────────────────────────────────────────────
    -- METRICS
    -- Same units as forecast_vintages so `actual - forecast` is trivial.
    -- Fractional is allowed because downstream attribution models
    -- produce non-integer leads.
    -- ────────────────────────────────────────────────────────────────
    leads               NUMERIC,
    ad_spend            NUMERIC,
    -- ad_spend is NULL for paid_channel='organic' (organic has no spend).
    -- A CHECK constraint would be tempting here, but would block the
    -- "spend shows up a day late in the data source" scenario. Enforce
    -- in the ingest script instead.

    -- ────────────────────────────────────────────────────────────────
    -- AUDIT / PROVENANCE
    -- ────────────────────────────────────────────────────────────────
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    ingested_by         TEXT,
    -- Who/what wrote the row. Typical values:
    --   'edge-fn:daily-actuals-worker', 'github-actions', 'backfill-2026-04-21'
    source              TEXT,
    -- Where the numbers came from. e.g.:
    --   'looker:daily_lead_actuals_v2',
    --   'derived:leads_weekly_actuals',
    --   'api:google_ads+salesforce'
    -- Important for reconciling discrepancies later.

    notes               TEXT,
    -- Freeform, optional.

    -- ────────────────────────────────────────────────────────────────
    -- VERSIONING (late-arriving data / restatements)
    -- Mirrors forecast_vintages. Normal daily ingest writes is_current
    -- = TRUE. If Looker restates numbers for a past day (common in week
    -- 1-2), the ingest script flips the old rows and writes new ones.
    -- ────────────────────────────────────────────────────────────────
    is_current          BOOLEAN     NOT NULL DEFAULT TRUE,
    superseded_at       TIMESTAMPTZ,
    superseded_by       BIGINT REFERENCES leads_daily_actuals(id),
    supersede_reason    TEXT
);

-- ──────────────────────────────────────────────────────────────────
-- INDEXES
-- ──────────────────────────────────────────────────────────────────

-- Enforces "only one CURRENT row per grain". Historical (is_current=FALSE)
-- rows stack freely so restatements preserve history.
CREATE UNIQUE INDEX leads_daily_actuals_current_uq
    ON leads_daily_actuals (
        actual_date, business, lead_source, audience, paid_channel
    )
    WHERE is_current = TRUE;

-- Hot query path: forecast-vs-actual joins on date.
CREATE INDEX leads_daily_actuals_actual_date_idx
    ON leads_daily_actuals (actual_date);

-- Hot query path: "paid spend efficiency by BU × channel" dashboards.
CREATE INDEX leads_daily_actuals_bu_channel_idx
    ON leads_daily_actuals (business, lead_source, paid_channel);

-- ──────────────────────────────────────────────────────────────────
-- COLUMN COMMENTS (self-documenting in Supabase UI + psql)
-- ──────────────────────────────────────────────────────────────────

COMMENT ON TABLE  leads_daily_actuals IS
    'Daily actuals (leads + ad_spend) at BU × lead_source × audience × paid_channel grain. Companion to forecast_vintages for forecast-vs-actual analysis. See supabase/migrations/2026-04-21_leads_daily_actuals.sql.';

COMMENT ON COLUMN leads_daily_actuals.actual_date   IS 'Calendar day covered by these actuals. Distinct name from forecast_vintages.forecast_date to surface mistaken joins.';
COMMENT ON COLUMN leads_daily_actuals.paid_channel  IS 'paid | organic. Absent from forecast_vintages (plan is all-in on spend); present here because efficiency analysis requires the split.';
COMMENT ON COLUMN leads_daily_actuals.source        IS 'Provenance tag so restatements can be traced back to the originating pipeline (looker:..., derived:..., api:...).';
COMMENT ON COLUMN leads_daily_actuals.is_current    IS 'TRUE for the active version of this grain. Flipped to FALSE when a later ingest restates the day.';

-- ──────────────────────────────────────────────────────────────────
-- CONVENIENCE VIEWS
-- ──────────────────────────────────────────────────────────────────

CREATE VIEW leads_daily_actuals_current AS
    SELECT
        id, actual_date, business, lead_source, audience, paid_channel,
        leads, ad_spend, ingested_at, source
    FROM leads_daily_actuals
    WHERE is_current = TRUE;

COMMENT ON VIEW leads_daily_actuals_current IS
    'Active version of each actuals row. Use this instead of the base table unless you specifically want historical (superseded) versions.';

-- Forecast-vs-actual at the plan's native grain. Joins CURRENT versions
-- on both sides and collapses paid_channel (since forecast_vintages has
-- no equivalent dimension today — if/when it does, drop the SUM()).
CREATE VIEW forecast_vs_actual_daily AS
    SELECT
        fv.plan_type,
        fv.plan_start_date,
        fv.forecast_date                                  AS date,
        fv.business,
        fv.lead_source,
        fv.audience,
        fv.leads                                          AS forecast_leads,
        fv.ad_spend                                       AS forecast_ad_spend,
        act.actual_leads,
        act.actual_ad_spend,
        act.actual_leads    - fv.leads                    AS leads_diff,
        act.actual_ad_spend - fv.ad_spend                 AS ad_spend_diff
    FROM forecast_vintages_current fv
    LEFT JOIN (
        SELECT
            actual_date, business, lead_source, audience,
            SUM(leads)    AS actual_leads,
            SUM(ad_spend) AS actual_ad_spend
        FROM leads_daily_actuals_current
        GROUP BY 1, 2, 3, 4
    ) act
      ON act.actual_date  = fv.forecast_date
     AND act.business     = fv.business
     AND act.lead_source  = fv.lead_source
     AND act.audience     = fv.audience;

COMMENT ON VIEW forecast_vs_actual_daily IS
    'Forecast vs actual at the plan''s daily × BU × lead_source × audience grain. Paid/organic collapsed via SUM; drop the subquery if forecast_vintages adds paid_channel.';

COMMIT;


-- =====================================================================
-- REFERENCE: common query patterns (informational; not executed)
-- =====================================================================

-- Daily miss/beat for a locked plan:
-- SELECT date, business, SUM(leads_diff) AS leads_diff_total
-- FROM forecast_vs_actual_daily
-- WHERE plan_type = 'SOP'
--   AND plan_start_date = '2026-04-26'
--   AND date <= CURRENT_DATE
-- GROUP BY 1, 2
-- ORDER BY 1, 2;

-- Paid-only efficiency (CPL) by BU for a week:
-- SELECT business, lead_source,
--        SUM(ad_spend) AS spend,
--        SUM(leads)    AS leads,
--        ROUND(SUM(ad_spend) / NULLIF(SUM(leads), 0), 2) AS cpl
-- FROM leads_daily_actuals_current
-- WHERE paid_channel = 'paid'
--   AND actual_date BETWEEN '2026-04-26' AND '2026-05-02'
-- GROUP BY 1, 2
-- ORDER BY 1, 2;
