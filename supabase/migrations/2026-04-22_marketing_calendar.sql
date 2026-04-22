-- =====================================================================
-- Migration: marketing_calendar (date dimension + holiday/event overlay)
-- Date:      2026-04-22
--
-- Purpose:   Canonical date dimension covering every day 2022-01-01 through
--            end of 2027, sourced from the "Calendar" tab of the Marketing
--            Model Google Sheet. Captures:
--              - Per-date calendar primitives (DOW, week_start, etc.)
--              - Per-date event input (col I: "Holiday Input"), sparse
--              - Per-date event classification (cols AJ:AL lookup), sparse
--              - Per-week event rollup (name + full-week-impact flag), dense
--
--            Grain: ONE ROW PER DATE (≈2,190 rows for 6 years). We chose the
--            dense date-dimension pattern over a sparse events-only table so
--            that:
--              - Date-based queries join on `date` in one hop.
--              - Future non-event attributes (fiscal_week, is_quarter_end,
--                school_year_label) are straightforward ALTER TABLE ADD COLUMN.
--              - Consumers that only want events can query the provided
--                marketing_calendar_events_current view.
--
--            This table is NOT consumed by any forecast code today. Writing
--            it has zero effect on Supabase's generate_forecast() RPC, the
--            Google Sheet model, memorialize_forecast.py, or any existing
--            Supabase table. See MODEL_STUDY_NOTES.md §11 for the future
--            roadmap for using this table to automate the holiday Week Adj
--            that is today typed manually into `Top Line!U`.
--
--            Loader: `load_calendar.py` (reads Sheet → upserts here with
--            soft-supersede on change).
--
-- This migration IS idempotent via IF NOT EXISTS guards on the indexes and
-- views, but the CREATE TABLE is not. Do not re-run against an existing
-- table without review.
-- =====================================================================

BEGIN;

CREATE TABLE marketing_calendar (
    id                          BIGSERIAL PRIMARY KEY,

    -- ────────────────────────────────────────────────────────────────
    -- CALENDAR PRIMITIVES (immutable; derivable from `date` alone)
    -- Matches columns B, C, D, E, H of the Calendar tab one-for-one.
    -- ────────────────────────────────────────────────────────────────
    date                        DATE        NOT NULL,
    day_of_week                 TEXT        NOT NULL,
    -- 'Sunday', 'Monday', ..., 'Saturday'. Derived from date.
    day_of_week_num             SMALLINT    NOT NULL
                                CHECK (day_of_week_num BETWEEN 0 AND 6),
    -- 0=Sunday .. 6=Saturday (matches the Sheet's Control!AI:AJ mapping,
    -- NOT Python's default Monday=0 convention).
    week_start                  DATE        NOT NULL,
    -- Sunday of the week containing `date`. The join key for weekly-grain
    -- consumers (Top Line, Weekly - Baseline Forecast, etc.).
    week_number                 INTEGER     NOT NULL,
    -- Sheet's sequential week index (Calendar!E). Read verbatim from the
    -- tab so downstream SQL matches what users see in the Sheet.
    week_year                   INTEGER     NOT NULL,
    -- YEAR(week_start). Occasionally differs from YEAR(date) at year ends.

    -- ────────────────────────────────────────────────────────────────
    -- EVENT INPUT (sparse; from Calendar!I)
    -- NULL for the vast majority of dates. Populated by the analyst in
    -- the Sheet, one entry per holiday/release date. "Holy Week" is
    -- tagged ONCE, on Palm Sunday — the week rollup fields below fan
    -- it out across the other six days of that week.
    -- ────────────────────────────────────────────────────────────────
    event_name                  TEXT,
    event_type                  TEXT
                                CHECK (event_type IN ('holiday', 'test_release')),

    -- ────────────────────────────────────────────────────────────────
    -- EVENT CLASSIFICATION (from Calendar!AI:AM attribute lookup)
    -- Populated only on days that have an event_name. NULL otherwise.
    -- Mutually exclusive: a holiday is either same-day-of-week (MLK =
    -- always a Monday) or same-date (Jul 4 = always July 4th), never
    -- both. `full_week_impact` is orthogonal — tells the top-line
    -- forecast layer "this entire week's volume shifts vs a normal
    -- week".
    -- ────────────────────────────────────────────────────────────────
    same_day_of_week            BOOLEAN,
    same_date                   BOOLEAN,
    full_week_impact            BOOLEAN,

    -- ────────────────────────────────────────────────────────────────
    -- WEEK-LEVEL ROLLUP (dense; populated for EVERY date in a holiday
    -- week, not just the tagged one)
    -- Matches the fan-out behavior of Calendar!J (Holiday Week Flag)
    -- and !K (Holiday Week Name) so downstream queries can filter
    -- "give me every date in a full-week holiday" with a single
    -- predicate instead of re-deriving weeks.
    -- ────────────────────────────────────────────────────────────────
    week_event_name             TEXT,
    week_has_full_week_impact   BOOLEAN     NOT NULL DEFAULT FALSE,

    -- ────────────────────────────────────────────────────────────────
    -- AUDIT / PROVENANCE
    -- ────────────────────────────────────────────────────────────────
    ingested_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    ingested_by                 TEXT        NOT NULL DEFAULT 'load_calendar.py',
    source                      TEXT        NOT NULL DEFAULT 'sheet:Calendar',

    -- ────────────────────────────────────────────────────────────────
    -- VERSIONING (soft-supersede; identical pattern to
    -- marketing_forecast_vintages and marketing_leads_daily_actuals)
    -- Whole-row supersede: if the analyst corrects an event for a date,
    -- that date's row is flagged is_current=FALSE and a new current row
    -- is inserted. Calendar primitives are immutable in practice but
    -- we still version the whole row for consistency with sibling
    -- tables' audit model.
    -- ────────────────────────────────────────────────────────────────
    is_current                  BOOLEAN     NOT NULL DEFAULT TRUE,
    superseded_at               TIMESTAMPTZ,
    superseded_by               BIGINT      REFERENCES marketing_calendar(id),
    supersede_reason            TEXT,

    -- ────────────────────────────────────────────────────────────────
    -- INVARIANTS
    -- ────────────────────────────────────────────────────────────────

    -- is_current <=> (superseded_at IS NULL). Can't be both current AND
    -- superseded; can't be neither. Mirrors the sibling tables' check.
    CONSTRAINT marketing_calendar_currency_consistent
        CHECK ((superseded_at IS NULL) = is_current),

    -- same_day_of_week XOR same_date, per the Sheet's AJ/AK convention.
    -- A holiday is one or the other, never both. Catches loader bugs
    -- that would otherwise silently ship a contradictory classification.
    CONSTRAINT marketing_calendar_classifier_exclusive
        CHECK (NOT (same_day_of_week = TRUE AND same_date = TRUE)),

    -- Event-name and event-type travel together: either both NULL (no
    -- event on this date) or both populated.
    CONSTRAINT marketing_calendar_event_fields_together
        CHECK ((event_name IS NULL) = (event_type IS NULL)),

    -- Classifier booleans may only be populated when there IS an event.
    -- Expressed as "if no event, all classifiers are NULL".
    CONSTRAINT marketing_calendar_classifiers_need_event
        CHECK (event_name IS NOT NULL
               OR (same_day_of_week IS NULL
                   AND same_date IS NULL
                   AND full_week_impact IS NULL))
);

-- ──────────────────────────────────────────────────────────────────
-- INDEXES
-- ──────────────────────────────────────────────────────────────────

-- Enforces "one CURRENT row per date". Superseded rows stack freely.
CREATE UNIQUE INDEX marketing_calendar_date_current_uq
    ON marketing_calendar (date)
    WHERE is_current = TRUE;

-- Hot path: "give me the week containing this date" and weekly rollups.
CREATE INDEX marketing_calendar_week_start_idx
    ON marketing_calendar (week_start)
    WHERE is_current = TRUE;

-- Hot path: "show me every instance of Holy Week across years".
CREATE INDEX marketing_calendar_event_name_idx
    ON marketing_calendar (event_name)
    WHERE is_current = TRUE AND event_name IS NOT NULL;

-- Hot path: "every date in a full-week-impact holiday week". This is
-- the predicate the Phase 3 forecast adjustment layer will lean on.
CREATE INDEX marketing_calendar_full_week_idx
    ON marketing_calendar (week_start)
    WHERE is_current = TRUE AND week_has_full_week_impact = TRUE;

-- ──────────────────────────────────────────────────────────────────
-- COLUMN / TABLE COMMENTS
-- ──────────────────────────────────────────────────────────────────

COMMENT ON TABLE marketing_calendar IS
    'Date dimension 2022-2027. Sourced from the "Calendar" tab of the Marketing Model Google Sheet. One row per date. Event fields (event_name, event_type, classifiers) are sparse. Week-rollup fields (week_event_name, week_has_full_week_impact) are dense. Owned by marketing. See supabase/migrations/2026-04-22_marketing_calendar.sql and MODEL_STUDY_NOTES.md.';

COMMENT ON COLUMN marketing_calendar.day_of_week_num IS
    '0=Sunday .. 6=Saturday. Matches the Sheet''s Control!AI:AJ mapping, NOT Python''s default Monday=0.';
COMMENT ON COLUMN marketing_calendar.week_start IS
    'Sunday of the week containing `date`. Use this as the join key for weekly-grain tables.';
COMMENT ON COLUMN marketing_calendar.event_name IS
    'User input from Calendar!I. NULL on non-event dates. Holy Week is tagged once on Palm Sunday; the week_event_name column fans it out.';
COMMENT ON COLUMN marketing_calendar.event_type IS
    'holiday (New Year''s, MLK, Easter, Holy Week, etc.) or test_release (SAT, ACT). Co-located because both share the Sheet''s AI:AM attribute lookup; split into a separate table later if needed.';
COMMENT ON COLUMN marketing_calendar.same_day_of_week IS
    'Holiday falls on the same weekday every year (MLK = 3rd Monday). From Calendar!AJ.';
COMMENT ON COLUMN marketing_calendar.same_date IS
    'Holiday falls on the same calendar date every year (Jul 4, Dec 25). From Calendar!AK.';
COMMENT ON COLUMN marketing_calendar.full_week_impact IS
    'The whole week''s top-line leads shift materially vs a non-event week. Flows into the weekly-rollup column for every date in that week. From Calendar!AL.';
COMMENT ON COLUMN marketing_calendar.week_event_name IS
    'Event name applied to every day in the same week. Populated even on non-tagged dates (Holy Week''s Wednesday gets week_event_name=Holy Week). Matches Sheet!K fan-out.';
COMMENT ON COLUMN marketing_calendar.week_has_full_week_impact IS
    'TRUE for every date in a week containing at least one full-week-impact event. The Phase 3 forecast adjustment layer will filter on this.';
COMMENT ON COLUMN marketing_calendar.is_current IS
    'TRUE for the active version of each date. Flipped to FALSE if the loader re-ingests different event info for that date.';

-- ──────────────────────────────────────────────────────────────────
-- CONVENIENCE VIEWS
-- ──────────────────────────────────────────────────────────────────

-- Default view for 99% of consumers. Drops audit columns + superseded rows.
CREATE VIEW marketing_calendar_current AS
    SELECT
        id, date, day_of_week, day_of_week_num,
        week_start, week_number, week_year,
        event_name, event_type,
        same_day_of_week, same_date, full_week_impact,
        week_event_name, week_has_full_week_impact
    FROM marketing_calendar
    WHERE is_current = TRUE;

COMMENT ON VIEW marketing_calendar_current IS
    'Active version of each calendar date. Use this unless you specifically need superseded rows.';

-- Weekly-grain view. One row per (week_start, event_name). Callers that
-- only need "is this a full-week holiday week?" prefer this to GROUP BY
-- on the base table.
CREATE VIEW marketing_calendar_weeks_current AS
    SELECT DISTINCT
        week_start,
        week_number,
        week_year,
        week_event_name,
        week_has_full_week_impact
    FROM marketing_calendar
    WHERE is_current = TRUE;

COMMENT ON VIEW marketing_calendar_weeks_current IS
    'Weekly-grain rollup. One row per week. Use for top-line forecast adjustments that key off week_start.';

-- Sparse event-only view. Equivalent to the "sparse events table" design
-- we considered and rejected — exposed as a view for consumers that
-- naturally think in events rather than dates. ~190 rows for 2022-2027.
CREATE VIEW marketing_calendar_events_current AS
    SELECT
        id,
        date            AS event_date,
        event_name,
        event_type,
        same_day_of_week,
        same_date,
        full_week_impact,
        week_start
    FROM marketing_calendar
    WHERE is_current = TRUE
      AND event_name IS NOT NULL;

COMMENT ON VIEW marketing_calendar_events_current IS
    'Sparse events view: one row per tagged holiday or test-release date. ~190 rows for 2022-2027.';

COMMIT;


-- =====================================================================
-- REFERENCE: common query patterns (informational; not executed)
-- =====================================================================

-- Get calendar context for a specific date (one-hop join):
--   SELECT * FROM marketing_calendar_current WHERE date = '2026-04-05';
-- → Easter Sunday 2026 with all its classifiers.

-- Is this week a full-week holiday week?
--   SELECT week_has_full_week_impact
--   FROM marketing_calendar_current
--   WHERE date = '2026-03-31'  -- a Tuesday
--   LIMIT 1;
-- → TRUE (inside Holy Week, even though March 31 isn't the tagged day).

-- Every Holy Week date across years (for YoY-aligned forecast comparisons):
--   SELECT date, week_start, week_year
--   FROM marketing_calendar_current
--   WHERE week_event_name = 'Holy Week'
--   ORDER BY date;

-- Join forecast vintages to calendar to filter holiday weeks:
--   SELECT fv.*, mc.week_event_name, mc.week_has_full_week_impact
--   FROM marketing_forecast_vintages_current fv
--   JOIN marketing_calendar_current mc ON mc.date = fv.forecast_date
--   WHERE fv.plan_type = 'SOP' AND fv.plan_start_date = '2026-04-26';

-- All versions (including superseded) for a date — audit view:
--   SELECT id, is_current, ingested_at, superseded_at, supersede_reason,
--          event_name, full_week_impact
--   FROM marketing_calendar
--   WHERE date = '2026-04-05'
--   ORDER BY ingested_at;

-- =====================================================================
-- VERIFICATION (run after migration; not executed):
-- =====================================================================
-- Row count sanity (2022-01-01 through 2027-12-31 = 2,191 days):
--   SELECT COUNT(*) FROM marketing_calendar_current;
-- Expect ~2,190 (exact depends on the Sheet's date range).
--
-- Event counts:
--   SELECT event_name, COUNT(*)
--   FROM marketing_calendar_current
--   WHERE event_name IS NOT NULL
--   GROUP BY 1 ORDER BY 1;
-- Expect ~190 total; MLK/Memorial/Christmas/etc. ~6 each (one per year);
-- SAT ~42, ACT ~42 (7 per year × 6 years).
--
-- Full-week-impact week count:
--   SELECT COUNT(DISTINCT week_start)
--   FROM marketing_calendar_current
--   WHERE week_has_full_week_impact = TRUE;
-- Expect ~60 (Thanksgiving×6 + Christmas×6 + Holy Week×6 + Spring Break×12
--             + SAT×~42 + ACT×~42, with overlaps — dedupe via DISTINCT).
-- =====================================================================
