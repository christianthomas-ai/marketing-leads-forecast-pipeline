"""Load the Marketing Model "Calendar" tab into Supabase (marketing_calendar).

Reads three ranges from the live Google Sheet:

  1. Calendar!B2:I3000 — per-date rows:
       B = date (serial or ISO)
       D = week_start (Sunday of the week)
       E = week_number (Sheet's sequential index)
       H = week_year  (YEAR(week_start))
       I = Holiday Input (user-entered event name; sparse)
     (Columns C/F/G are recomputed in Python so the loader doesn't depend
      on the Sheet's exact DOW/formula semantics.)

  2. Calendar!R3:S3000 — test prep score release dates:
       R = ACT Test Score Release (TRUE on tagged dates)
       S = SAT Test Score Release (TRUE on tagged dates)
     Lead impact dates (V/W in the Sheet) are derived in Python as
     release_date + 7 days, matching the Sheet's formula chain.

  3. Calendar!AC1:AF40 — holiday/event attribute lookup:
       AC = holiday_name
       AD = same_day_of_week (bool)
       AE = same_date        (bool; Easter uses a formula that resolves)
       AF = full_week_impact (bool)

Writes to `marketing_calendar` using the soft-supersede pattern documented
in `supabase/migrations/2026-04-22_marketing_calendar.sql`:

  - First run: inserts every date row as `is_current=TRUE`.
  - Re-runs: compares each date's current row against the incoming row.
    If any of (event_name, event_type, same_day_of_week, same_date,
    full_week_impact, week_event_name, week_has_full_week_impact) changed,
    flips the old row to `is_current=FALSE` and inserts a new current row.
    Unchanged dates are skipped.

This loader DOES NOT affect any forecast. The `marketing_calendar` table
is currently read by nothing in the pipeline; see MODEL_STUDY_NOTES.md §11
for the planned consumers (Phase 3 Python adjustment layer).

Prerequisites
-------------
1. Apply the migration:
     supabase/migrations/2026-04-22_marketing_calendar.sql
2. .env / environment:
     SUPABASE_URL
     SUPABASE_SERVICE_ROLE_KEY
     GOOGLE_CREDS_FILE   (default: credentials.json)
     SPREADSHEET_NAME    (default: "Marketing Model - Live")
     CALENDAR_WORKSHEET_NAME (default: "Calendar")

Usage
-----
Dry run (read + diff; don't write):
    python load_calendar.py --dry-run

First-time backfill / re-run:
    python load_calendar.py --ingested-by christian.thomas

Full wipe and reload (supersede all existing, insert fresh):
    python load_calendar.py --replace-all --ingested-by christian.thomas

Explicit supersede reason (appended to rows that actually change):
    python load_calendar.py \\
        --ingested-by christian.thomas \\
        --supersede-reason "Analyst corrected 2026 Spring Break 2 date"
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

import httpx
from dotenv import load_dotenv

from read_from_sheets import read_range
from vintage_validation import parse_sheet_date

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
CALENDAR_WORKSHEET_NAME = os.environ.get("CALENDAR_WORKSHEET_NAME", "Calendar")

# Supabase table this loader owns.
CALENDAR_TABLE = "marketing_calendar"

# ── Sheet range bounds ───────────────────────────────────────────────
# Calendar tab has ~2,200 data rows through end-of-2027; cap well above
# that so the loader still picks up future-year extensions without edits.
_DATE_RANGE = "B2:I3000"
_RELEASE_RANGE = "R3:S3000"  # R=ACT release, S=SAT release (TRUE on tagged dates)
_LOOKUP_RANGE = "AC1:AF40"

# Names we classify as test_release rather than holiday. Driven by what
# the Sheet's AI:AM table actually contains — confirmed by MODEL_STUDY_NOTES.md.
# Additions to this set require a loader tweak AND (if you want
# downstream grouping to recognize them) nothing in the DB — the
# event_type check constraint has no new-value hurdle.
_TEST_RELEASE_NAMES = frozenset({"SAT", "ACT"})

# Day-of-week mapping to match Sheet's Control!AI:AJ (Sunday=0..Saturday=6),
# NOT Python's default Monday=0 convention.
_DOW_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday",
              "Thursday", "Friday", "Saturday"]


# ── MODELS ───────────────────────────────────────────────────────────

@dataclass
class CalendarRow:
    """One date's worth of calendar + event data, ready to insert."""

    date: str                                 # ISO YYYY-MM-DD
    day_of_week: str
    day_of_week_num: int                      # 0=Sun..6=Sat
    week_start: str                           # ISO YYYY-MM-DD
    week_number: int
    week_year: int
    event_name: Optional[str]
    event_type: Optional[str]                 # 'holiday' | 'test_release' | None
    same_day_of_week: Optional[bool]
    same_date: Optional[bool]
    full_week_impact: Optional[bool]
    week_event_name: Optional[str]
    week_has_full_week_impact: bool
    # Test prep score release / lead impact (parallel track to holidays)
    score_release_type: Optional[str]         # 'SAT' | 'ACT' | None
    score_impact_type: Optional[str]          # 'SAT' | 'ACT' | None (7 days after release)
    week_score_impact_name: Optional[str]     # weekly fan-out of impact
    week_has_score_impact: bool


# ── HELPERS ──────────────────────────────────────────────────────────

def _die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _validate_env() -> None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        _die("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in env/.env")


def _supabase_headers(prefer_return: bool = False) -> dict[str, str]:
    hdrs = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer_return:
        hdrs["Prefer"] = "return=representation"
    return hdrs


def _parse_bool(cell: Any) -> Optional[bool]:
    """Best-effort bool parse tolerating Sheet quirks.

    Returns None for empty / unparseable (caller decides whether that's a
    validation error — for our lookup table, empty = "not in the table").
    """
    if cell is None:
        return None
    if isinstance(cell, bool):
        return cell
    s = str(cell).strip().upper()
    if s == "":
        return None
    if s in ("TRUE", "1", "Y", "YES"):
        return True
    if s in ("FALSE", "0", "N", "NO"):
        return False
    return None


_REF_NAME = {"AJ": "same_day_of_week",
             "AK": "same_date",
             "AL": "full_week_impact",
             "AD": "same_day_of_week",
             "AE": "same_date",
             "AF": "full_week_impact"}


def _resolve_bool_or_formula(cell: Any, *, same_row_values: dict[str, Optional[bool]]) -> Optional[bool]:
    """Parse a cell that may be a bool, bool-string, or a known formula shape.

    The Calendar!AI:AM lookup is mostly literal TRUE/FALSE, with two
    formula shapes used heavily for the derived classifiers:

      AK (same_date) for most rows:
        =IF(AJ<n>=TRUE, FALSE, TRUE)            → NOT AJ

      AL (full_week_impact) for rows that DON'T hard-code TRUE:
        =IF(AND(AJ<n>=FALSE, AK<n>=FALSE), TRUE, FALSE)  → (NOT AJ) AND (NOT AK)

    Hard-coded TRUE AL cells (Thanksgiving, Christmas, Spring Break 1/2,
    Holy Week, SAT, ACT) pass through the literal-parsing branch.

    ``same_row_values`` supplies previously-resolved booleans from the
    same row, keyed by logical name ('same_day_of_week', 'same_date',
    'full_week_impact'), so these formulas can be evaluated symbolically.

    If the Sheet ever grows more formula shapes in this range, add a new
    branch rather than reaching for a general expression evaluator — we
    want the loader to fail loudly on unexpected input.
    """
    direct = _parse_bool(cell)
    if direct is not None:
        return direct

    if cell is None:
        return None
    s = str(cell).strip()
    if s == "" or not s.startswith("="):
        return None

    import re

    # Pattern 1: =IF(<ref>=TRUE, FALSE, TRUE) == NOT <ref>
    m = re.match(
        r"^=IF\(\s*(?P<ref>A[A-Z])\d+\s*=\s*TRUE\s*,\s*FALSE\s*,\s*TRUE\s*\)$",
        s,
        flags=re.IGNORECASE,
    )
    if m:
        ref_key = m.group("ref").upper()
        if ref_key not in _REF_NAME:
            return None
        ref_val = same_row_values.get(_REF_NAME[ref_key])
        if ref_val is None:
            return None
        return not ref_val

    # Pattern 2: =IF(AND(<ref1>=FALSE, <ref2>=FALSE), TRUE, FALSE)
    #         == (NOT <ref1>) AND (NOT <ref2>)
    m = re.match(
        r"^=IF\(\s*AND\(\s*"
        r"(?P<ref1>A[A-Z])\d+\s*=\s*FALSE\s*,\s*"
        r"(?P<ref2>A[A-Z])\d+\s*=\s*FALSE\s*"
        r"\)\s*,\s*TRUE\s*,\s*FALSE\s*\)$",
        s,
        flags=re.IGNORECASE,
    )
    if m:
        k1 = m.group("ref1").upper()
        k2 = m.group("ref2").upper()
        if k1 not in _REF_NAME or k2 not in _REF_NAME:
            return None
        r1 = same_row_values.get(_REF_NAME[k1])
        r2 = same_row_values.get(_REF_NAME[k2])
        if r1 is None or r2 is None:
            return None
        return (not r1) and (not r2)

    return None


def _compute_week_start(d: date) -> date:
    """Sunday of the week containing d (matches Sheet's Control!AI:AJ convention)."""
    # Python's weekday(): Mon=0..Sun=6. We want days since Sunday.
    days_since_sunday = (d.weekday() + 1) % 7
    return d - timedelta(days=days_since_sunday)


# ── SHEET READ ───────────────────────────────────────────────────────

def read_lookup_table() -> dict[str, dict[str, Optional[bool]]]:
    """Read Calendar!AI1:AM40 and return {name: {sdow, sdate, fwi}}.

    Uses render='formula' because the live model's whole-sheet recalc
    (triggered by unformatted/formatted) consistently times out on this
    range. Most cells are literal TRUE/FALSE/text; the one formula cell
    (Easter's AK = `=IF(AJ19=TRUE,FALSE,TRUE)` = NOT same_day_of_week) is
    handled via _resolve_bool_or_formula.
    """
    print(f"Reading lookup table from '{CALENDAR_WORKSHEET_NAME}'!{_LOOKUP_RANGE}...")
    grid = read_range(CALENDAR_WORKSHEET_NAME, _LOOKUP_RANGE, render="formula")

    if not grid:
        _die(f"'{CALENDAR_WORKSHEET_NAME}'!{_LOOKUP_RANGE} came back empty.")

    # First row is the header ("Holiday", "Same DOW", "Same Date",
    # "Full Week", "Count" or similar). Skip it; trust column order.
    lookup: dict[str, dict[str, Optional[bool]]] = {}
    for i, row in enumerate(grid[1:], start=2):
        if not row or not str(row[0]).strip():
            continue
        raw_name = str(row[0]).strip()
        # Skip rows whose "name" is actually the header label repeated
        # or a known column heading (defensive against range-offset drift).
        if raw_name.upper() in ("HOLIDAY", "EVENT", "NAME"):
            continue
        # Resolve classifiers left-to-right so AJ is available when AK's
        # formula (NOT AJ) is evaluated.
        sdow = _parse_bool(row[1]) if len(row) > 1 else None
        same_row = {"same_day_of_week": sdow,
                    "same_date": None, "full_week_impact": None}
        sdate = _resolve_bool_or_formula(row[2], same_row_values=same_row) \
            if len(row) > 2 else None
        same_row["same_date"] = sdate
        fwi = _resolve_bool_or_formula(row[3], same_row_values=same_row) \
            if len(row) > 3 else None

        # Sanity: need at least one classifier to be a real entry.
        if sdow is None and sdate is None and fwi is None:
            print(
                f"  WARNING: lookup row {i} for '{raw_name}' has no classifiers; skipping.",
                file=sys.stderr,
            )
            continue
        lookup[raw_name] = {
            "same_day_of_week": sdow,
            "same_date": sdate,
            "full_week_impact": fwi,
        }

    print(f"  Loaded {len(lookup)} holiday/event classifications.")
    return lookup


def read_date_rows() -> list[tuple[date, Optional[str]]]:
    """Read Calendar!B:I and return (date, event_name) tuples.

    Col B is a date chain: the first non-empty cell is a static serial
    (e.g. 44563 = 2022-01-02) and every subsequent cell is ``=B<prev>+1``.
    With render='formula' we only see the anchor serial; we walk the
    chain in Python, advancing the date by one per row. Rows where the
    chain breaks (blank col B, non-numeric, etc.) are treated as the
    end of the calendar.

    Col I (Holiday Input) is user-entered text and round-trips cleanly
    at any render mode. Blanks are skipped (→ event_name=None).

    Columns C/D/E/F/G/H are formula-derived in the Sheet; we ignore them
    and recompute day_of_week / week_start / week_number / week_year
    from the date in Python. This also means the loader works even if
    those columns change shape in the future.
    """
    print(f"Reading date rows from '{CALENDAR_WORKSHEET_NAME}'!{_DATE_RANGE}...")
    # render='formula' avoids the live-model whole-sheet recalc that
    # unformatted/formatted triggers (consistently times out).
    grid = read_range(CALENDAR_WORKSHEET_NAME, _DATE_RANGE, render="formula")

    rows: list[tuple[date, Optional[str]]] = []
    current_date: Optional[date] = None

    for raw in grid:
        if not raw:
            # Blank row: reset the chain. If it restarts later with another
            # static serial, we pick it back up.
            current_date = None
            continue

        b_cell = raw[0] if len(raw) > 0 else ""
        b_str = str(b_cell).strip() if b_cell != "" else ""

        if b_str == "":
            current_date = None
            continue

        # Static anchor: numeric serial or ISO string.
        if not b_str.startswith("="):
            try:
                iso = parse_sheet_date(b_cell)
                current_date = datetime.strptime(iso, "%Y-%m-%d").date()
            except ValueError:
                # Non-date content in B (header label etc.); skip without
                # breaking the chain we may have been walking.
                continue

        # Formula row: `=B<prev>+1`. Accept only the pattern we expect;
        # if we see anything else the chain is ambiguous and we stop
        # until the next anchor.
        elif b_str.startswith("="):
            import re
            m = re.match(r"^=B\d+\s*\+\s*1$", b_str, flags=re.IGNORECASE)
            if not m:
                # Unknown formula in col B — treat as chain break rather
                # than guess. Log once per run so the user can investigate.
                print(
                    f"  NOTE: col B has an unexpected formula: {b_str!r}; "
                    f"chain paused until next static anchor.",
                    file=sys.stderr,
                )
                current_date = None
                continue
            if current_date is None:
                # Formula cell but no anchor yet → skip.
                continue
            current_date = current_date + timedelta(days=1)
        else:
            continue

        event_name_raw = raw[7] if len(raw) > 7 else ""
        event_name = str(event_name_raw).strip() if event_name_raw else ""
        # Filter out header labels that sometimes re-appear mid-range.
        if event_name.lower() in {"holiday input", "holiday"}:
            event_name = ""
        rows.append((current_date, event_name or None))

    print(f"  Loaded {len(rows)} date rows "
          f"({rows[0][0].isoformat() if rows else 'none'} -> "
          f"{rows[-1][0].isoformat() if rows else 'none'}).")
    return rows


def read_score_releases() -> dict[date, str]:
    """Read Calendar!R:S and return {release_date: 'ACT'|'SAT'}.

    Col R = ACT Test Score Release (TRUE on tagged dates).
    Col S = SAT Test Score Release (TRUE on tagged dates).
    We walk the same date chain as read_date_rows to resolve row -> date.
    """
    print(f"Reading score release dates from '{CALENDAR_WORKSHEET_NAME}'!{_RELEASE_RANGE}...")
    grid = read_range(CALENDAR_WORKSHEET_NAME, _RELEASE_RANGE, render="formula")

    # We need to align rows with the date chain. _RELEASE_RANGE starts at
    # row 3 (first data row), same as _DATE_RANGE's data. We already know
    # the date chain starts at 2022-01-02 in row 3 and increments by 1 day.
    # Read the date column to get the anchor.
    date_grid = read_range(CALENDAR_WORKSHEET_NAME, "B3:B3", render="formula")
    anchor_raw = date_grid[0][0] if date_grid and date_grid[0] else None
    try:
        anchor_iso = parse_sheet_date(anchor_raw)
        anchor = datetime.strptime(anchor_iso, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        _die(f"Cannot parse Calendar!B3 anchor date: {anchor_raw!r}")
        return {}  # unreachable, _die exits

    releases: dict[date, str] = {}
    for i, row in enumerate(grid):
        d = anchor + timedelta(days=i)
        r_val = row[0] if len(row) > 0 else ""
        s_val = row[1] if len(row) > 1 else ""
        if r_val is True or (isinstance(r_val, str) and r_val.strip().upper() == "TRUE"):
            releases[d] = "ACT"
        if s_val is True or (isinstance(s_val, str) and s_val.strip().upper() == "TRUE"):
            releases[d] = "SAT"

    print(f"  Found {len(releases)} score release dates "
          f"({len([v for v in releases.values() if v == 'SAT'])} SAT, "
          f"{len([v for v in releases.values() if v == 'ACT'])} ACT).")
    return releases


# ── TRANSFORM ────────────────────────────────────────────────────────

def build_calendar_rows(
    date_rows: list[tuple[date, Optional[str]]],
    lookup: dict[str, dict[str, Optional[bool]]],
    score_releases: dict[date, str],
    *,
    strict: bool = False,
) -> list[CalendarRow]:
    """Two-pass transform: classify events, then fan out week rollups.

    Calendar primitives (day_of_week, week_start, week_number, week_year)
    are computed in Python rather than read from the Sheet, since the
    Sheet stores them as formulas and render='formula' can't evaluate them.
    """

    # ── Pass 1: per-date classification ───────────────────────────
    classified: list[CalendarRow] = []
    unknown_events: set[str] = set()
    for d, event_name in date_rows:
        computed_ws = _compute_week_start(d)
        dow_num = (d.weekday() + 1) % 7
        dow_name = _DOW_NAMES[dow_num]
        wk_year = computed_ws.year

        # Python-native week number: count Sundays since the first Sunday
        # on-or-before Jan 1 of wk_year. Matches Calendar!E for most years
        # and is stable across year boundaries (Jan 1 2023 falls in week 1
        # which has Dec 25-31 2022 in the Sheet's convention too).
        jan1 = date(wk_year, 1, 1)
        first_sunday = jan1 - timedelta(days=(jan1.weekday() + 1) % 7)
        wk_num = ((computed_ws - first_sunday).days // 7) + 1

        # Classify the event (if any).
        if event_name:
            if event_name in _TEST_RELEASE_NAMES:
                event_type: Optional[str] = "test_release"
            else:
                event_type = "holiday"
            attrs = lookup.get(event_name)
            if attrs is None:
                unknown_events.add(event_name)
                if strict:
                    _die(
                        f"Event '{event_name}' on {d.isoformat()} has no entry "
                        f"in Calendar!AI:AM. Add it to the lookup table or pass "
                        f"--strict=false to accept with NULL classifiers."
                    )
                sdow = sdate = fwi = None
            else:
                sdow = attrs["same_day_of_week"]
                sdate = attrs["same_date"]
                fwi = attrs["full_week_impact"]
        else:
            event_type = None
            sdow = sdate = fwi = None

        # Test prep: release is tagged on the actual date; impact is 7 days later.
        release_type = score_releases.get(d)
        # Check if THIS date is the impact date for a release 7 days ago.
        impact_source = d - timedelta(days=7)
        impact_type = score_releases.get(impact_source)

        classified.append(CalendarRow(
            date=d.isoformat(),
            day_of_week=dow_name,
            day_of_week_num=dow_num,
            week_start=computed_ws.isoformat(),
            week_number=wk_num,
            week_year=wk_year,
            event_name=event_name,
            event_type=event_type,
            same_day_of_week=sdow,
            same_date=sdate,
            full_week_impact=fwi,
            week_event_name=None,             # filled in pass 2
            week_has_full_week_impact=False,  # filled in pass 2
            score_release_type=release_type,
            score_impact_type=impact_type,
            week_score_impact_name=None,      # filled in pass 2
            week_has_score_impact=False,       # filled in pass 2
        ))

    if unknown_events:
        print(
            f"  WARNING: {len(unknown_events)} event name(s) not in Calendar!AI:AM "
            f"lookup; their classifier booleans are NULL. Add them to the "
            f"attribute table if classification matters: {sorted(unknown_events)}",
            file=sys.stderr,
        )

    # ── Pass 2: week-level fan-out ────────────────────────────────
    # Group by week_start. For each week, pick the "primary" event
    # (earliest-date-tagged, per Sheet!K INDEX/FILTER semantics), and
    # set week_has_full_week_impact TRUE if ANY event in that week is.
    by_week: dict[str, list[CalendarRow]] = defaultdict(list)
    for r in classified:
        by_week[r.week_start].append(r)

    for week_start, rows in by_week.items():
        # ── Holiday fan-out ──
        events = sorted(
            (r for r in rows if r.event_name),
            key=lambda r: r.date,
        )
        if events:
            primary_name = events[0].event_name
            any_fwi = any(r.full_week_impact is True for r in events)
            for r in rows:
                r.week_event_name = primary_name
                r.week_has_full_week_impact = any_fwi

        # ── Test prep score impact fan-out ──
        impacts = sorted(
            (r for r in rows if r.score_impact_type),
            key=lambda r: r.date,
        )
        if impacts:
            primary_impact = impacts[0].score_impact_type
            for r in rows:
                r.week_score_impact_name = primary_impact
                r.week_has_score_impact = True

    return classified


# ── SUPABASE READ ────────────────────────────────────────────────────

_PAGE_SIZE = 1000  # PostgREST default cap.


def fetch_current_calendar(*, tolerate_missing: bool = False) -> dict[str, dict[str, Any]]:
    """Return {date: row_dict} for every current row in marketing_calendar.

    If ``tolerate_missing`` is True and the table doesn't exist yet (e.g.
    the migration hasn't been applied), returns an empty dict instead of
    dying. Useful for dry-runs that run before the first migration.
    """
    print(f"Fetching current rows from {CALENDAR_TABLE}...")
    all_rows: list[dict[str, Any]] = []
    offset = 0
    select = ",".join([
        "id", "date", "event_name", "event_type",
        "same_day_of_week", "same_date", "full_week_impact",
        "week_event_name", "week_has_full_week_impact",
        "score_release_type", "score_impact_type",
        "week_score_impact_name", "week_has_score_impact",
    ])
    while True:
        resp = httpx.get(
            f"{SUPABASE_URL}/rest/v1/{CALENDAR_TABLE}",
            headers=_supabase_headers(),
            params={
                "select": select,
                "is_current": "eq.true",
                "limit": _PAGE_SIZE,
                "offset": offset,
            },
            timeout=60,
        )
        if resp.status_code >= 400:
            # Postgres "undefined_table" surfaces through PostgREST either
            # as a 404 with code "PGRST205" (schema cache miss on a
            # missing table) or as an error body containing "42P01" /
            # "does not exist". In dry-run, treat either as "no existing
            # rows" so the loader is useful before the migration lands.
            body = resp.text or ""
            table_missing = (
                resp.status_code == 404
                or "PGRST205" in body
                or "42P01" in body
                or "could not find the table" in body.lower()
                or "does not exist" in body.lower()
            )
            if tolerate_missing and table_missing:
                print(
                    f"  {CALENDAR_TABLE} not found yet (migration not applied). "
                    f"Treating as empty for dry-run."
                )
                return {}
            _die(f"Fetch current failed: HTTP {resp.status_code} — {resp.text}")
        batch = resp.json()
        if not batch:
            break
        all_rows.extend(batch)
        if len(batch) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    print(f"  Fetched {len(all_rows)} current rows.")
    return {r["date"]: r for r in all_rows}


# ── DIFF ─────────────────────────────────────────────────────────────

# Fields we actually care about for change detection. Calendar primitives
# (day_of_week, week_start, etc.) are functions of `date` and can't drift,
# so we don't diff on them.
_DIFF_FIELDS = (
    "event_name", "event_type",
    "same_day_of_week", "same_date", "full_week_impact",
    "week_event_name", "week_has_full_week_impact",
    "score_release_type", "score_impact_type",
    "week_score_impact_name", "week_has_score_impact",
)


def row_differs(incoming: CalendarRow, existing: dict[str, Any]) -> bool:
    for field in _DIFF_FIELDS:
        a = getattr(incoming, field)
        b = existing.get(field)
        # Normalize NULL vs None
        if a == b:
            continue
        # Handle boolean None/False asymmetry Supabase sometimes returns
        if a is None and b is None:
            continue
        return True
    return False


# ── WRITE ────────────────────────────────────────────────────────────

def insert_rows(rows: Iterable[CalendarRow], ingested_by: str) -> list[dict[str, Any]]:
    """Insert new current rows. Returns the inserted records with ids."""
    payload = [
        {
            "date": r.date,
            "day_of_week": r.day_of_week,
            "day_of_week_num": r.day_of_week_num,
            "week_start": r.week_start,
            "week_number": r.week_number,
            "week_year": r.week_year,
            "event_name": r.event_name,
            "event_type": r.event_type,
            "same_day_of_week": r.same_day_of_week,
            "same_date": r.same_date,
            "full_week_impact": r.full_week_impact,
            "week_event_name": r.week_event_name,
            "week_has_full_week_impact": r.week_has_full_week_impact,
            "score_release_type": r.score_release_type,
            "score_impact_type": r.score_impact_type,
            "week_score_impact_name": r.week_score_impact_name,
            "week_has_score_impact": r.week_has_score_impact,
            "ingested_by": ingested_by,
        }
        for r in rows
    ]
    if not payload:
        return []
    # Chunk inserts to keep request bodies reasonable (1000/req mirrors
    # PostgREST page size; well under any URL/body caps).
    inserted: list[dict[str, Any]] = []
    for start in range(0, len(payload), _PAGE_SIZE):
        chunk = payload[start:start + _PAGE_SIZE]
        resp = httpx.post(
            f"{SUPABASE_URL}/rest/v1/{CALENDAR_TABLE}",
            headers=_supabase_headers(prefer_return=True),
            json=chunk,
            timeout=120,
        )
        if resp.status_code >= 400:
            _die(f"Insert failed: HTTP {resp.status_code} — {resp.text}")
        inserted.extend(resp.json())
    return inserted


def supersede_rows(ids: list[int], reason: Optional[str]) -> None:
    """Flip rows to is_current=FALSE with reason (and set superseded_at=now())."""
    if not ids:
        return
    payload = {
        "is_current": False,
        "superseded_at": "now()",
        "supersede_reason": reason,
    }
    # PATCH in chunks, same reason as inserts.
    for start in range(0, len(ids), _PAGE_SIZE):
        chunk = ids[start:start + _PAGE_SIZE]
        resp = httpx.patch(
            f"{SUPABASE_URL}/rest/v1/{CALENDAR_TABLE}",
            headers=_supabase_headers(),
            params={"id": f"in.({','.join(str(i) for i in chunk)})"},
            json=payload,
            timeout=60,
        )
        if resp.status_code >= 400:
            _die(f"Supersede PATCH failed: HTTP {resp.status_code} — {resp.text}")


def patch_superseded_by(old_ids: list[int], new_id: int) -> None:
    """Point every superseded row at a single representative replacement id."""
    if not old_ids:
        return
    for start in range(0, len(old_ids), _PAGE_SIZE):
        chunk = old_ids[start:start + _PAGE_SIZE]
        resp = httpx.patch(
            f"{SUPABASE_URL}/rest/v1/{CALENDAR_TABLE}",
            headers=_supabase_headers(),
            params={"id": f"in.({','.join(str(i) for i in chunk)})"},
            json={"superseded_by": new_id},
            timeout=60,
        )
        if resp.status_code >= 400:
            _die(f"superseded_by PATCH failed: HTTP {resp.status_code} — {resp.text}")


# ── MAIN ─────────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--ingested-by",
        default="load_calendar.py",
        help="Identity for the ingested_by audit column (e.g. christian.thomas).",
    )
    p.add_argument(
        "--supersede-reason",
        default=None,
        help="Reason attached to rows that get superseded on this run. "
             "Required if any rows will be superseded.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Read + diff but do not write to Supabase.",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Error out on event_names not present in Calendar!AI:AM "
             "(default: warn and proceed with NULL classifiers).",
    )
    p.add_argument(
        "--replace-all",
        action="store_true",
        help="Supersede ALL existing current rows and insert fresh. "
             "Use for full reloads after schema changes or sheet restructures.",
    )
    return p


def _summarize_changes(
    incoming: list[CalendarRow],
    existing: dict[str, dict[str, Any]],
) -> tuple[list[CalendarRow], list[tuple[CalendarRow, dict[str, Any]]], int]:
    """Return (to_insert, to_update, unchanged_count).

    to_insert: dates with no existing current row (first-time insert).
    to_update: (incoming, existing) pairs where something changed.
    unchanged: count of dates where current matches incoming exactly.
    """
    to_insert: list[CalendarRow] = []
    to_update: list[tuple[CalendarRow, dict[str, Any]]] = []
    unchanged = 0
    for row in incoming:
        existing_row = existing.get(row.date)
        if existing_row is None:
            to_insert.append(row)
        elif row_differs(row, existing_row):
            to_update.append((row, existing_row))
        else:
            unchanged += 1
    return to_insert, to_update, unchanged


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    _validate_env()

    # 1. Read Sheet.
    lookup = read_lookup_table()
    date_rows = read_date_rows()
    score_releases = read_score_releases()
    if not date_rows:
        _die("No date rows read from the Calendar tab.")

    # 2. Transform.
    calendar_rows = build_calendar_rows(date_rows, lookup, score_releases,
                                        strict=args.strict)

    # 3. Diff against existing current rows. Dry-runs tolerate a missing
    # table so this loader can be validated before the migration is applied.
    existing = fetch_current_calendar(tolerate_missing=args.dry_run)
    to_insert, to_update, unchanged = _summarize_changes(calendar_rows, existing)

    # Report per-category event counts for a human sanity check.
    event_counts: dict[str, int] = defaultdict(int)
    for r in calendar_rows:
        if r.event_name:
            event_counts[r.event_name] += 1
    fwi_weeks = len({r.week_start for r in calendar_rows if r.week_has_full_week_impact})
    score_impact_weeks = len({r.week_start for r in calendar_rows if r.week_has_score_impact})
    release_count = sum(1 for r in calendar_rows if r.score_release_type)
    impact_count = sum(1 for r in calendar_rows if r.score_impact_type)

    print("\n-- Summary --------------------------------------------")
    print(f"  Total dates:                {len(calendar_rows)}")
    print(f"  Dates with events:          {sum(1 for r in calendar_rows if r.event_name)}")
    print(f"  Distinct event names:       {len(event_counts)}")
    print(f"  Weeks with full-week-impact: {fwi_weeks}")
    print(f"  Score release dates:        {release_count}")
    print(f"  Score impact dates:         {impact_count}")
    print(f"  Weeks with score impact:    {score_impact_weeks}")
    print(f"  Unchanged vs Supabase:      {unchanged}")
    print(f"  New inserts:                {len(to_insert)}")
    print(f"  Supersedes:                 {len(to_update)}")
    if event_counts:
        print("\n  Event counts (top 25):")
        for name, n in sorted(event_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:25]:
            print(f"    {name:<25} {n}")

    # --replace-all: treat every existing row as needing supersede, and
    # every incoming row as a fresh insert. Ignores the field-level diff.
    if args.replace_all and existing:
        reason = args.supersede_reason or "Full reload via --replace-all"
        to_insert = calendar_rows
        to_update = []
        unchanged = 0
        print(f"\n  --replace-all: will supersede all {len(existing)} existing rows "
              f"and insert {len(to_insert)} fresh rows.")

    # Bail early if nothing to do.
    if not to_insert and not to_update:
        print("\nNo changes. Exiting.")
        return 0

    # Require a supersede reason if we're changing anything previously locked.
    if to_update and not args.supersede_reason and not args.dry_run:
        _die(
            f"{len(to_update)} existing row(s) would be superseded but "
            f"--supersede-reason was not provided. Pass one to confirm the change."
        )

    if args.dry_run:
        print("\n[DRY RUN] No writes.")
        if to_update:
            print(f"  First 10 of {len(to_update)} proposed supersedes:")
            for row, prev in to_update[:10]:
                diffs = [
                    (field, prev.get(field), getattr(row, field))
                    for field in _DIFF_FIELDS
                    if prev.get(field) != getattr(row, field)
                ]
                diffs_str = ", ".join(f"{f}: {b!r} -> {a!r}" for f, b, a in diffs)
                print(f"    {row.date}: {diffs_str}")
        if to_insert:
            # For backfills, show the first and last few dates plus a sample
            # of event-bearing rows so the analyst can eyeball classifications.
            print(f"  First 5 of {len(to_insert)} proposed inserts:")
            for r in to_insert[:5]:
                print(f"    {r.date} {r.day_of_week:<9} ws={r.week_start} "
                      f"event={r.event_name or '-':<18} "
                      f"fwi={r.full_week_impact}")
            print(f"  Sample event rows (up to 10):")
            events_sample = [r for r in to_insert if r.event_name][:10]
            for r in events_sample:
                print(f"    {r.date} {r.event_name:<18} type={r.event_type:<12} "
                      f"sdow={r.same_day_of_week} sdate={r.same_date} "
                      f"fwi={r.full_week_impact} week_fwi={r.week_has_full_week_impact}")
        return 0

    # 4a. --replace-all: supersede old rows FIRST to clear the unique
    # index (one current row per date), then insert fresh.
    if args.replace_all and existing:
        old_ids = [r["id"] for r in existing.values()]
        reason = args.supersede_reason or "Full reload via --replace-all"
        supersede_rows(old_ids, reason)
        print(f"\n  Superseded {len(old_ids)} old rows (--replace-all).")
        to_write = calendar_rows
        inserted = insert_rows(to_write, ingested_by=args.ingested_by)
        print(f"  Inserted {len(inserted)} new rows.")
        if inserted:
            anchor_id = min(r["id"] for r in inserted)
            patch_superseded_by(old_ids, anchor_id)
            print(f"  Linked superseded rows to anchor id {anchor_id}.")
    else:
        # Normal path: insert first (new dates + replacements), then supersede.
        to_write = list(to_insert) + [row for row, _ in to_update]
        inserted = insert_rows(to_write, ingested_by=args.ingested_by)
        print(f"\n  Inserted {len(inserted)} new rows.")
        if to_update:
            old_ids = [prev["id"] for _, prev in to_update]
            supersede_rows(old_ids, args.supersede_reason)
            anchor_id = min(r["id"] for r in inserted)
            patch_superseded_by(old_ids, anchor_id)
            print(f"  Superseded {len(old_ids)} old rows (anchor id {anchor_id}).")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
