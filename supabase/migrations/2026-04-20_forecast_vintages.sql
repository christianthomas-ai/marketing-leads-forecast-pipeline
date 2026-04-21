-- =====================================================================
-- Migration: forecast_vintages (initial)
-- Date:      2026-04-20
-- NOTE:      This table and its convenience view were renamed to
--            marketing_forecast_vintages / marketing_forecast_vintages_current
--            on 2026-04-21 to namespace the marketing-owned objects ahead
--            of finance adding their own tables. See
--            supabase/migrations/2026-04-21_rename_marketing_tables.sql.
--            This file is kept verbatim as history; if you drop-and-recreate
--            in dev, apply both migrations in order.
--
-- Purpose:   Create the system-of-record table for locked marketing plans
--            (Sales & Operating Plans, Quarterly Operating Plans, etc.).
--            Replaces the "Daily Forecast - LOCKED For Summary Tables" tab
--            in the Marketing Model Google Sheet as the canonical store.
--
-- Shape mirrors the existing Sheet tab one-for-one so memorialization
-- continues unchanged; new audit/versioning columns make the re-lock
-- edge case explicit and preserve history.
--
-- This migration is NOT idempotent. Do not re-run without reviewing.
-- If you need to start over in dev: DROP TABLE forecast_vintages CASCADE;
-- =====================================================================

BEGIN;

CREATE TABLE forecast_vintages (
    -- Surrogate primary key. Lets re-locks (soft-overwrite) insert new
    -- rows with the same business-key combo without constraint violations.
    id                  BIGSERIAL PRIMARY KEY,

    -- ────────────────────────────────────────────────────────────────
    -- PLAN IDENTITY (matches the Sheet tab one-for-one)
    -- ────────────────────────────────────────────────────────────────
    plan_type           TEXT        NOT NULL,
    -- e.g. 'SOP'  = Sales & Operating Plan (weekly)
    --      'Q2OP' = Q2 Operating Plan (quarterly lock)
    --      future values welcome; intentionally not a CHECK constraint

    plan_start_date     DATE        NOT NULL,
    -- The Sunday the plan goes into effect. If a plan is locked on
    -- Sunday 2026-04-19, plan_start_date is typically 2026-04-26 (the
    -- following week).

    forecast_date       DATE        NOT NULL,
    -- Daily forecast grain. One row per (plan, day, BU, channel, audience).

    business            TEXT        NOT NULL,
    -- 'VT Core' | 'International' | 'Prof Certs'

    lead_source         TEXT        NOT NULL,
    -- Uses the naming convention of the Sheet tab (NOT the Looker or
    -- 'Model Lead Source' variants). Examples:
    --   'Brand & Direct', 'Other', 'Phone',
    --   'Search - Non Tutor', 'Search - Tutor PPC', 'Search - Tutor SEO'
    -- Expanded values are fine; keep the Sheet as source of truth.

    audience            TEXT        NOT NULL,
    -- e.g. 'Col-STEM', 'Grad Test Prep', 'HS-STEM', 'K-6', 'K12 Test Prep',
    --      'Languages', 'Learning Differences', 'Other', 'Upskilling',
    --      'Unknown', 'International-CAN', 'Prof Certs'
    -- Prof Certs rows legitimately have business='Prof Certs' and
    -- audience='Prof Certs' (collapsed dimension); this is expected.

    -- ────────────────────────────────────────────────────────────────
    -- FORECAST METRICS
    -- Start with what the Sheet tab has today; add columns with
    -- ALTER TABLE when new metrics are memorialized (tROAS target,
    -- ROAS bidding target, etc.). See project manifest §3 & §5.
    -- ────────────────────────────────────────────────────────────────
    leads               NUMERIC,       -- fractional ok (daily splits of weekly totals)
    ad_spend            NUMERIC,       -- fractional ok

    -- ────────────────────────────────────────────────────────────────
    -- AUDIT / PROVENANCE
    -- Things the Sheet can't store natively; critical for accuracy
    -- tracking and stakeholder trust.
    -- ────────────────────────────────────────────────────────────────
    plan_locked_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- When the row was written. For the initial backfill, use the
    -- plan_start_date (or an explicit historical timestamp) so we don't
    -- lose fidelity on when plans were actually locked.

    locked_by           TEXT,
    -- Identity of the locker. Typical values: 'christian.thomas',
    -- 'github-actions', 'backfill-2026-04-20'. Free-text for now.

    notes               TEXT,
    -- Freeform. Short context that's useful later ("rollback fix for
    -- Meta mapping", "pre-holiday adjusted", "locked early due to outage").

    -- ────────────────────────────────────────────────────────────────
    -- VERSIONING (soft-overwrite for the re-lock edge case)
    -- Normal flow: every lock inserts new rows with is_current = TRUE.
    -- Re-lock flow: old rows get flagged is_current = FALSE and point
    -- at their replacement via superseded_by; new rows inserted with
    -- is_current = TRUE. Nothing is ever physically deleted.
    -- ────────────────────────────────────────────────────────────────
    is_current          BOOLEAN     NOT NULL DEFAULT TRUE,
    superseded_at       TIMESTAMPTZ,
    superseded_by       BIGINT REFERENCES forecast_vintages(id),
    supersede_reason    TEXT
);

-- ──────────────────────────────────────────────────────────────────
-- INDEXES
-- ──────────────────────────────────────────────────────────────────

-- Enforces "only one CURRENT row per business-key combo". Historical
-- (is_current = FALSE) rows are not constrained and can stack freely.
CREATE UNIQUE INDEX forecast_vintages_current_uq
    ON forecast_vintages (
        plan_type, plan_start_date, forecast_date,
        business, lead_source, audience
    )
    WHERE is_current = TRUE;

-- Hot query path: "show me everything from plan X locked on date Y"
CREATE INDEX forecast_vintages_plan_idx
    ON forecast_vintages (plan_type, plan_start_date);

-- Hot query path: "what was forecast for this day across all plans?"
CREATE INDEX forecast_vintages_forecast_date_idx
    ON forecast_vintages (forecast_date);

-- Hot query path: "show me this BU × channel across all vintages"
CREATE INDEX forecast_vintages_bu_channel_idx
    ON forecast_vintages (business, lead_source);

-- ──────────────────────────────────────────────────────────────────
-- COLUMN COMMENTS (self-documenting in the Supabase UI + psql)
-- ──────────────────────────────────────────────────────────────────

COMMENT ON TABLE  forecast_vintages IS
    'System-of-record for locked marketing plans. Replaces the "Daily Forecast - LOCKED For Summary Tables" tab in the Marketing Model Google Sheet. See supabase/migrations/2026-04-20_forecast_vintages.sql for design notes.';

COMMENT ON COLUMN forecast_vintages.plan_type        IS 'Plan category, e.g. SOP, Q2OP. Free-text (no CHECK constraint); add new values freely.';
COMMENT ON COLUMN forecast_vintages.plan_start_date  IS 'The Sunday the plan goes into effect. Distinct from plan_locked_at.';
COMMENT ON COLUMN forecast_vintages.forecast_date    IS 'Daily forecast grain (the date being forecasted).';
COMMENT ON COLUMN forecast_vintages.lead_source      IS 'Follows the Sheet tab naming convention (e.g. "Search - Tutor PPC"), NOT the Looker or Model mapping variants.';
COMMENT ON COLUMN forecast_vintages.is_current       IS 'TRUE for the active version of each business-key combo. Flipped to FALSE on re-lock.';
COMMENT ON COLUMN forecast_vintages.superseded_at    IS 'When the row stopped being current (re-lock replaced it).';
COMMENT ON COLUMN forecast_vintages.superseded_by    IS 'ID of the row that replaced this one. NULL for current rows.';
COMMENT ON COLUMN forecast_vintages.supersede_reason IS 'Short human-readable reason for the re-lock (e.g. "Meta mapping bug, re-locked 4-21").';

-- ──────────────────────────────────────────────────────────────────
-- CONVENIENCE VIEW
-- 90%+ of downstream queries only care about the current version.
-- Query this instead of the base table in those cases.
-- ──────────────────────────────────────────────────────────────────

CREATE VIEW forecast_vintages_current AS
    SELECT
        id, plan_type, plan_start_date, forecast_date,
        business, lead_source, audience,
        leads, ad_spend,
        plan_locked_at, locked_by, notes
    FROM forecast_vintages
    WHERE is_current = TRUE;

COMMENT ON VIEW forecast_vintages_current IS
    'Active version of each plan row. Use this instead of the base table unless you specifically want historical (superseded) versions.';

COMMIT;


-- =====================================================================
-- REFERENCE: common query patterns (informational; not executed)
-- =====================================================================

-- Show one plan's full contents:
-- SELECT *
-- FROM forecast_vintages_current
-- WHERE plan_type = 'SOP'
--   AND plan_start_date = '2026-04-26'
-- ORDER BY forecast_date, business, lead_source, audience;

-- Weekly rollup for a plan (leads by BU):
-- SELECT forecast_date, business, SUM(leads) AS leads
-- FROM forecast_vintages_current
-- WHERE plan_type = 'SOP' AND plan_start_date = '2026-04-26'
-- GROUP BY 1, 2
-- ORDER BY 1, 2;

-- All versions (including superseded) for a specific combo — audit view:
-- SELECT id, is_current, plan_locked_at, superseded_at, supersede_reason, leads, ad_spend
-- FROM forecast_vintages
-- WHERE plan_type       = 'SOP'
--   AND plan_start_date = '2026-04-26'
--   AND forecast_date   = '2026-05-04'
--   AND business        = 'VT Core'
--   AND lead_source     = 'Search - Tutor PPC'
--   AND audience        = 'HS-STEM'
-- ORDER BY plan_locked_at;

-- =====================================================================
-- REFERENCE: upsert pattern the Python writer will use
-- =====================================================================
-- Pseudocode (for a "normal new lock", not a re-lock):
--
--   INSERT INTO forecast_vintages
--     (plan_type, plan_start_date, forecast_date,
--      business,  lead_source,     audience,
--      leads,     ad_spend,        locked_by, notes)
--   VALUES (...);
--
-- For a re-lock, the writer will:
--   1. UPDATE forecast_vintages
--        SET is_current = FALSE,
--            superseded_at = now(),
--            supersede_reason = :reason
--      WHERE plan_type = :pt
--        AND plan_start_date = :psd
--        AND is_current = TRUE;
--   2. INSERT the new rows (is_current defaults to TRUE).
--   3. UPDATE the superseded rows' superseded_by to point at the new ids.
-- =====================================================================
