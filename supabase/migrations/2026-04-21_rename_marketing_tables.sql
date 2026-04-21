-- =====================================================================
-- Migration: rename forecast_vintages / leads_daily_actuals to
--            marketing_* namespace
-- Date:      2026-04-21
-- Purpose:   This Supabase project is being used for all of finance, not
--            just marketing. Rename the marketing-owned tables and views
--            up front (before other domains land their own tables) so
--            the ownership boundary is obvious in the Supabase UI.
--
-- Safety: ALTER ... RENAME is a metadata-only operation in Postgres. No
-- row data is touched, no locks beyond a very brief ACCESS EXCLUSIVE,
-- dependent views/FKs are automatically updated by OID so nothing
-- breaks. Total runtime on 175k rows: <100ms.
--
-- After this runs, the ONLY external change needed is to the Python
-- scripts + GitHub Actions workflow that reference the old names; those
-- are updated in the same commit as this migration.
--
-- This migration is NOT idempotent. Do not re-run. If a table with the
-- target name already exists the ALTER will fail fast.
-- =====================================================================

BEGIN;

-- ──────────────────────────────────────────────────────────────────
-- 1. Drop the dependent view first. Renaming the underlying tables
--    would work (PG tracks deps by OID), but we also want to rename
--    THIS view, and you can't CREATE OR REPLACE a view through a name
--    change. Easiest path: drop here, recreate under the new name at
--    the bottom of the migration.
-- ──────────────────────────────────────────────────────────────────

DROP VIEW IF EXISTS forecast_vs_actual_daily;

-- ──────────────────────────────────────────────────────────────────
-- 2. Rename the "current" views next (they depend on the base tables
--    by OID, so renaming the tables doesn't invalidate them, but we
--    want their NAMES namespaced too).
-- ──────────────────────────────────────────────────────────────────

ALTER VIEW forecast_vintages_current
    RENAME TO marketing_forecast_vintages_current;

ALTER VIEW leads_daily_actuals_current
    RENAME TO marketing_leads_daily_actuals_current;

-- ──────────────────────────────────────────────────────────────────
-- 3. Rename the base tables.
-- ──────────────────────────────────────────────────────────────────

ALTER TABLE forecast_vintages   RENAME TO marketing_forecast_vintages;
ALTER TABLE leads_daily_actuals RENAME TO marketing_leads_daily_actuals;

-- ──────────────────────────────────────────────────────────────────
-- 4. Rename indexes. Existing index names are legacy-prefixed; the
--    indexes themselves keep working because PG resolves them by OID,
--    but renaming keeps `\d marketing_forecast_vintages` output
--    consistent with the new table name.
-- ──────────────────────────────────────────────────────────────────

ALTER INDEX forecast_vintages_current_uq
    RENAME TO marketing_forecast_vintages_current_uq;
ALTER INDEX forecast_vintages_plan_idx
    RENAME TO marketing_forecast_vintages_plan_idx;
ALTER INDEX forecast_vintages_forecast_date_idx
    RENAME TO marketing_forecast_vintages_forecast_date_idx;
ALTER INDEX forecast_vintages_bu_channel_idx
    RENAME TO marketing_forecast_vintages_bu_channel_idx;

ALTER INDEX leads_daily_actuals_current_uq
    RENAME TO marketing_leads_daily_actuals_current_uq;
ALTER INDEX leads_daily_actuals_actual_date_idx
    RENAME TO marketing_leads_daily_actuals_actual_date_idx;
ALTER INDEX leads_daily_actuals_bu_channel_idx
    RENAME TO marketing_leads_daily_actuals_bu_channel_idx;

-- ──────────────────────────────────────────────────────────────────
-- 5. Rename sequences and primary-key / foreign-key constraints. Not
--    strictly required (they resolve by OID), but leaving them with
--    the legacy names is confusing when debugging later.
-- ──────────────────────────────────────────────────────────────────

ALTER SEQUENCE forecast_vintages_id_seq
    RENAME TO marketing_forecast_vintages_id_seq;
ALTER SEQUENCE leads_daily_actuals_id_seq
    RENAME TO marketing_leads_daily_actuals_id_seq;

ALTER TABLE marketing_forecast_vintages
    RENAME CONSTRAINT forecast_vintages_pkey
    TO marketing_forecast_vintages_pkey;
ALTER TABLE marketing_forecast_vintages
    RENAME CONSTRAINT forecast_vintages_superseded_by_fkey
    TO marketing_forecast_vintages_superseded_by_fkey;

ALTER TABLE marketing_leads_daily_actuals
    RENAME CONSTRAINT leads_daily_actuals_pkey
    TO marketing_leads_daily_actuals_pkey;
ALTER TABLE marketing_leads_daily_actuals
    RENAME CONSTRAINT leads_daily_actuals_superseded_by_fkey
    TO marketing_leads_daily_actuals_superseded_by_fkey;

-- ──────────────────────────────────────────────────────────────────
-- 6. Refresh the table / view COMMENTs so the Supabase UI shows the
--    new names in descriptions (the old COMMENTs hardcoded
--    "forecast_vintages" etc. in prose).
-- ──────────────────────────────────────────────────────────────────

COMMENT ON TABLE  marketing_forecast_vintages IS
    'System-of-record for locked marketing plans. Replaces the "Daily Forecast - LOCKED For Summary Tables" tab in the Marketing Model Google Sheet. Owned by marketing. See supabase/migrations/2026-04-20_forecast_vintages.sql for design notes and 2026-04-21_rename_marketing_tables.sql for namespace history.';

COMMENT ON VIEW   marketing_forecast_vintages_current IS
    'Active version of each plan row. Use this instead of the base table unless you specifically want historical (superseded) versions.';

COMMENT ON TABLE  marketing_leads_daily_actuals IS
    'Daily actuals (leads + ad_spend) at BU x lead_source x audience x paid_channel grain. Companion to marketing_forecast_vintages for forecast-vs-actual analysis. Owned by marketing. See supabase/migrations/2026-04-21_leads_daily_actuals.sql.';

COMMENT ON VIEW   marketing_leads_daily_actuals_current IS
    'Active version of each actuals row. Use this instead of the base table unless you specifically want historical (superseded) versions.';

-- ──────────────────────────────────────────────────────────────────
-- 7. Recreate the forecast-vs-actual view under the new name. Body
--    is identical to the original; only the FROM / JOIN targets
--    change to reference the renamed views.
-- ──────────────────────────────────────────────────────────────────

CREATE VIEW marketing_forecast_vs_actual_daily AS
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
    FROM marketing_forecast_vintages_current fv
    LEFT JOIN (
        SELECT
            actual_date, business, lead_source, audience,
            SUM(leads)    AS actual_leads,
            SUM(ad_spend) AS actual_ad_spend
        FROM marketing_leads_daily_actuals_current
        GROUP BY 1, 2, 3, 4
    ) act
      ON act.actual_date  = fv.forecast_date
     AND act.business     = fv.business
     AND act.lead_source  = fv.lead_source
     AND act.audience     = fv.audience;

COMMENT ON VIEW marketing_forecast_vs_actual_daily IS
    'Forecast vs actual at the plan''s daily x BU x lead_source x audience grain. Paid/organic collapsed via SUM; drop the subquery if marketing_forecast_vintages adds paid_channel.';

COMMIT;


-- =====================================================================
-- VERIFICATION (run after migration; not executed):
-- =====================================================================
-- Sanity check that renames landed and row counts are unchanged:
--
-- SELECT 'marketing_forecast_vintages'   AS tbl, COUNT(*) FROM marketing_forecast_vintages
-- UNION ALL
-- SELECT 'marketing_leads_daily_actuals', COUNT(*) FROM marketing_leads_daily_actuals;
-- -- Expect 175015 / 0 (or whatever the pre-rename counts were).
--
-- Confirm no legacy names remain as relations:
--
-- SELECT relname
-- FROM pg_class
-- WHERE relname IN (
--     'forecast_vintages',
--     'forecast_vintages_current',
--     'leads_daily_actuals',
--     'leads_daily_actuals_current',
--     'forecast_vs_actual_daily'
-- );
-- -- Expect 0 rows.
--
-- Confirm the view still joins correctly (this is the main "did anything
-- break" check):
--
-- SELECT COUNT(*) FROM marketing_forecast_vs_actual_daily;
-- -- Expect ~same count as marketing_forecast_vintages_current.
-- =====================================================================
