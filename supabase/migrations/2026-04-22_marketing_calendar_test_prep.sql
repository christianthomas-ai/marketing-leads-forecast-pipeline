-- =====================================================================
-- Migration: marketing_calendar — add test prep score release/impact track
-- Date:      2026-04-22
--
-- Adds four columns for the SAT/ACT score release and lead impact
-- data from the Calendar tab's R-Z columns. These run PARALLEL to
-- the existing holiday track (event_name, full_week_impact, etc.)
-- because a date can have both a holiday AND a test prep impact.
--
-- After applying, re-run load_calendar.py to populate these columns.
-- =====================================================================

BEGIN;

ALTER TABLE marketing_calendar
    ADD COLUMN IF NOT EXISTS score_release_type TEXT
        CHECK (score_release_type IN ('SAT', 'ACT')),
    ADD COLUMN IF NOT EXISTS score_impact_type TEXT
        CHECK (score_impact_type IN ('SAT', 'ACT')),
    ADD COLUMN IF NOT EXISTS week_score_impact_name TEXT,
    ADD COLUMN IF NOT EXISTS week_has_score_impact BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN marketing_calendar.score_release_type IS
    'SAT or ACT on the actual score release date (from Calendar!R/S). NULL for most dates.';
COMMENT ON COLUMN marketing_calendar.score_impact_type IS
    'SAT or ACT on the lead impact date (7 days after release, from Calendar!V/W). NULL for most dates.';
COMMENT ON COLUMN marketing_calendar.week_score_impact_name IS
    'Score impact name fanned out to every day in the impact week. Parallel to week_event_name but for test prep.';
COMMENT ON COLUMN marketing_calendar.week_has_score_impact IS
    'TRUE for every date in a week containing a score impact. Parallel to week_has_full_week_impact.';

-- Index for "give me all weeks with a test prep score impact"
CREATE INDEX IF NOT EXISTS marketing_calendar_score_impact_idx
    ON marketing_calendar (week_start)
    WHERE is_current = TRUE AND week_has_score_impact = TRUE;

-- Update the _current view to include the new columns.
DROP VIEW IF EXISTS marketing_calendar_events_current;
DROP VIEW IF EXISTS marketing_calendar_weeks_current;
DROP VIEW IF EXISTS marketing_calendar_current;

CREATE VIEW marketing_calendar_current AS
    SELECT
        id, date, day_of_week, day_of_week_num,
        week_start, week_number, week_year,
        event_name, event_type,
        same_day_of_week, same_date, full_week_impact,
        week_event_name, week_has_full_week_impact,
        score_release_type, score_impact_type,
        week_score_impact_name, week_has_score_impact
    FROM marketing_calendar
    WHERE is_current = TRUE;

COMMENT ON VIEW marketing_calendar_current IS
    'Active version of each calendar date. Use this unless you specifically need superseded rows.';

CREATE VIEW marketing_calendar_weeks_current AS
    SELECT DISTINCT
        week_start,
        week_number,
        week_year,
        week_event_name,
        week_has_full_week_impact,
        week_score_impact_name,
        week_has_score_impact
    FROM marketing_calendar
    WHERE is_current = TRUE;

COMMENT ON VIEW marketing_calendar_weeks_current IS
    'Weekly-grain rollup. One row per week. Includes both holiday and test prep impact flags.';

CREATE VIEW marketing_calendar_events_current AS
    SELECT
        id,
        date            AS event_date,
        event_name,
        event_type,
        same_day_of_week,
        same_date,
        full_week_impact,
        week_start,
        score_release_type,
        score_impact_type
    FROM marketing_calendar
    WHERE is_current = TRUE
      AND (event_name IS NOT NULL OR score_release_type IS NOT NULL OR score_impact_type IS NOT NULL);

COMMENT ON VIEW marketing_calendar_events_current IS
    'Sparse events view: holidays + test prep release/impact dates.';

COMMIT;
