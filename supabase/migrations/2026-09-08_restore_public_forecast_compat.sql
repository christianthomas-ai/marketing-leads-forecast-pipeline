-- =====================================================================
-- Restore public forecast objects for marketing-leads-forecast-pipeline
-- Date: 2026-09-08
--
-- Context: On 2026-07-14 the Looker tables that the GitHub daily push
-- depended on (leads_weekly_actuals, week_calendar, weekly_leads_by_bu,
-- leads_forecast) left public. generate_forecast() was left behind and
-- the daily push_forecast.yml workflow was deleted on 2026-07-31 after
-- two weeks of 404s.
--
-- This migration does not revive the Looker wipe-reload Edge Function
-- (bright-worker). Actuals now come from Command Center pipelines that
-- already refresh in Business Planning:
--   - cc_starburst_leads_daily_current  (lead volume, 2022-12-31 → today)
--   - cc_kpi_all_business_looker        (paid mix / ad spend, 2024+)
--   - looker.week_calendar              (Sunday-start week index)
--
-- Views are security_invoker + not auto-updatable (JOIN) so a leftover
-- webhook cannot DELETE through them into Command Center tables.
-- Access is service_role only.
-- =====================================================================

BEGIN;

CREATE OR REPLACE VIEW public.week_calendar
  WITH (security_invoker = true) AS
SELECT week_start, week_year, week_number
FROM looker.week_calendar;

COMMENT ON VIEW public.week_calendar IS
    'Compatibility view for generate_forecast(). Source: looker.week_calendar (moved out of public 2026-07-14).';

CREATE OR REPLACE VIEW public.leads_weekly_actuals
  WITH (security_invoker = true) AS
SELECT
    k.ref_date AS "Reporting Date",
    wc.week_start AS "Reporting Week",
    k.business AS "Business",
    k.audience_subject AS "Audience (Sales)",
    k.lead_source_group AS "Lead Source Group",
    COALESCE(k.ad_spend, 0)::text AS "Ad Spend  (Total, incl VSX)",
    k.valid_leads::numeric AS "Leads (Valid)"
FROM public.cc_kpi_all_business_looker k
JOIN looker.week_calendar wc
  ON k.ref_date >= wc.week_start
 AND k.ref_date < (wc.week_start + 7)
WHERE k.business IN ('VT Core', 'International', 'Prof Certs');

COMMENT ON VIEW public.leads_weekly_actuals IS
    'Compatibility view for push_to_sheets.py freshness + paid-mix. Source: cc_kpi_all_business_looker. Not wipe-reloadable.';

CREATE OR REPLACE VIEW public.weekly_leads_by_bu
  WITH (security_invoker = true) AS
WITH weekly AS (
    SELECT
        CASE
            WHEN s.business LIKE 'International%' THEN 'International'
            ELSE s.business
        END AS "Business",
        wc.week_start,
        wc.week_year,
        wc.week_number,
        SUM(s.valid_leads)::numeric AS total_leads
    FROM public.cc_starburst_leads_daily_current s
    JOIN looker.week_calendar wc
      ON s.reporting_date >= wc.week_start
     AND s.reporting_date < (wc.week_start + 7)
    WHERE CASE
            WHEN s.business LIKE 'International%' THEN 'International'
            ELSE s.business
          END IN ('VT Core', 'International', 'Prof Certs')
    GROUP BY 1, 2, 3, 4
)
SELECT
    w.*,
    CASE
        WHEN LAG(w.total_leads) OVER (PARTITION BY w."Business" ORDER BY w.week_start) > 0
        THEN w.total_leads
             / LAG(w.total_leads) OVER (PARTITION BY w."Business" ORDER BY w.week_start)
             * 100
    END AS wow_pct
FROM weekly w;

COMMENT ON VIEW public.weekly_leads_by_bu IS
    'Weekly BU lead totals + WoW for generate_forecast(). Source: cc_starburst_leads_daily_current. International-* rolled into International.';

CREATE TABLE IF NOT EXISTS public.leads_forecast (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    "Business" TEXT NOT NULL,
    week_number INTEGER,
    week_year INTEGER,
    week_start DATE,
    py_leads NUMERIC,
    prior_week_forecast NUMERIC,
    avg_wow_3yr NUMERIC,
    trailing_4wk_yoy NUMERIC,
    baseline_forecast NUMERIC,
    final_forecast NUMERIC,
    forecast_adj NUMERIC DEFAULT 0,
    is_actual BOOLEAN,
    actual_leads NUMERIC,
    UNIQUE ("Business", week_year, week_number)
);

COMMENT ON TABLE public.leads_forecast IS
    'Baseline weekly leads forecast written by public.generate_forecast(). Recreated 2026-09-08 after the original public table was dropped.';

ALTER TABLE public.leads_forecast ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.week_calendar FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE public.leads_weekly_actuals FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE public.weekly_leads_by_bu FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE public.leads_forecast FROM PUBLIC, anon, authenticated;

GRANT SELECT ON TABLE public.week_calendar TO service_role;
GRANT SELECT ON TABLE public.leads_weekly_actuals TO service_role;
GRANT SELECT ON TABLE public.weekly_leads_by_bu TO service_role;
GRANT ALL ON TABLE public.leads_forecast TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.leads_forecast_id_seq TO service_role;

REVOKE EXECUTE ON FUNCTION public.generate_forecast() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.generate_forecast() TO postgres, service_role;

NOTIFY pgrst, 'reload schema';

COMMIT;
