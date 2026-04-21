"""Memorialize a locked forecast plan from the Google Sheet into Supabase.

Reads the locked Sheet tab (default: "Daily Forecast - LOCKED For Summary
Tables") from the Marketing Model Google Sheet, validates it, and upserts to
the `marketing_forecast_vintages` table in Supabase. Implements the re-lock
soft-overwrite flow documented in
supabase/migrations/2026-04-20_forecast_vintages.sql +
supabase/migrations/2026-04-21_rename_marketing_tables.sql.

The Sheet tab is a **multi-plan paste-values archive** that holds the current
SOP + current OP + current SOPM + recent historical SOPs, all stacked
(``Weekly - Totals`` references it by plan_start_date). memorialize narrows
down to one plan using `--plan-type` and an auto-detected "newest
plan_start_date" for that plan_type.

Prerequisites
-------------
1. Apply these migrations to Supabase in order before running:
     supabase/migrations/2026-04-20_forecast_vintages.sql
     supabase/migrations/2026-04-21_leads_daily_actuals.sql
     supabase/migrations/2026-04-21_rename_marketing_tables.sql
2. The .env file / environment must have:
     SUPABASE_URL
     SUPABASE_SERVICE_ROLE_KEY
     GOOGLE_CREDS_FILE   (defaults to ./credentials.json)
     SPREADSHEET_NAME    (defaults to "Marketing Model - Live")
     LOCKED_WORKSHEET_NAME (defaults to "Daily Forecast - LOCKED For Summary Tables")

Usage
-----
Weekly happy path — pick the newest plan_start_date for the given plan_type:

    python memorialize_forecast.py --plan-type SOP --locked-by christian.thomas

Pin a specific plan_start_date (for historical re-memorialization or if the
"newest" heuristic picks the wrong row):

    python memorialize_forecast.py \\
        --plan-type SOP --plan-start-date 2026-04-26 \\
        --locked-by christian.thomas \\
        --notes "Weekly SOP lock"

Re-lock (overrides the existing current vintage for the same plan):

    python memorialize_forecast.py --plan-type SOP --locked-by christian.thomas \\
        --supersede-reason "Meta mapping bug, re-locked 4-21"

Dry run (read + validate but don't write anything):

    python memorialize_forecast.py --plan-type SOP --locked-by christian.thomas --dry-run

Assumptions about the source tab (verify with ``python read_from_sheets.py
"<tab-name>" --head 5``):

- First non-blank row (within the top 5 rows) is headers.
- Data columns (in any order, case-insensitive): ``plan_type``,
  ``plan_start_date``, ``forecast_date`` (or ``Date``), ``business``,
  ``lead_source`` (or ``Channel``), ``audience``, ``leads``, ``ad_spend``.
- Many plans may be stacked. memorialize only writes the one you point it at
  via --plan-type (and --plan-start-date if you want to pin the vintage).
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import asdict, dataclass
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

from read_from_sheets import read_worksheet
from vintage_validation import (
    ValidationError,
    apply_aliases,
    find_header_row,
    normalize_header,
    parse_date_str,
    parse_float_maybe,
    parse_sheet_date,
    validate_rows,
)

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
# Default points at the paste-values archive the user already maintains: one
# tab with the current SOP + current OP + current SOPM + historical SOPs
# stacked. memorialize narrows down to the target plan via --plan-type (and
# --plan-start-date, if explicitly overridden) — see resolve_plan_identity().
LOCKED_WORKSHEET_NAME = os.environ.get(
    "LOCKED_WORKSHEET_NAME", "Daily Forecast - LOCKED For Summary Tables"
)

# Table name in Supabase. Exposed as a constant so test fixtures / future
# migrations can monkey-patch without a sweep of string literals.
VINTAGES_TABLE = "marketing_forecast_vintages"

REQUIRED_COLUMNS = {
    "plan_type",
    "plan_start_date",
    "forecast_date",
    "business",
    "lead_source",
    "audience",
    "leads",
    "ad_spend",
}


# ── MODELS ───────────────────────────────────────────────────────────

@dataclass
class VintageRow:
    """One grain-level row, ready to insert into marketing_forecast_vintages."""

    forecast_date: str  # ISO YYYY-MM-DD
    business: str
    lead_source: str
    audience: str
    leads: Optional[float]
    ad_spend: Optional[float]


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


# ── STEP 0: RESOLVE PLAN IDENTITY (tab scan + filter) ────────────────

def _scan_plans_in_grid(grid: list[list[str]]) -> dict[tuple[str, str], int]:
    """Return {(plan_type, plan_start_date): row_count} for every plan in the grid.

    Used by both the auto-detect path (expects exactly one plan) and by the
    "no match" error message (lists what WAS in the tab). Header row is found
    via the same find_header_row() logic as read_sheet_rows, so callers get
    the same behavior.
    """
    header_idx = find_header_row(grid, REQUIRED_COLUMNS)
    header = apply_aliases(normalize_header(grid[header_idx]))
    col_idx = {name: header.index(name) for name in REQUIRED_COLUMNS}

    counts: dict[tuple[str, str], int] = {}
    for raw in grid[header_idx + 1 :]:
        if not any(str(c).strip() for c in raw):
            continue
        pt = str(raw[col_idx["plan_type"]]).strip() if col_idx["plan_type"] < len(raw) else ""
        psd_raw = str(raw[col_idx["plan_start_date"]]).strip() if col_idx["plan_start_date"] < len(raw) else ""
        if not pt or not psd_raw:
            continue
        try:
            psd = parse_sheet_date(psd_raw)
        except ValueError:
            continue  # silently skip malformed plan_start_date cells during scan
        counts[(pt, psd)] = counts.get((pt, psd), 0) + 1
    return counts


def resolve_plan_identity(
    plan_type: Optional[str], plan_start_date: Optional[str]
) -> tuple[str, str]:
    """Resolve (plan_type, plan_start_date) against what actually lives in the tab.

    Resolution rules (in priority order):

    1. Both args given → return them as-is, no tab scan.
    2. --plan-type given, --plan-start-date omitted → scan the tab, pick the
       **max plan_start_date** observed for that plan_type. This is the weekly
       happy path: user pasted this week's SOP into the archive, runs
       ``--plan-type SOP`` and the script picks up the newest row set.
    3. --plan-start-date given, --plan-type omitted → scan the tab; if
       exactly one plan_type has that date, use it. If multiple, error.
       (Rare edge case; supported for symmetry.)
    4. Neither given → error with the full list of plans in the tab so the
       user can copy-paste the right --plan-type invocation.

    On "no matching plan in the tab" errors, we print the full inventory too,
    which is the single most useful diagnostic 9 times out of 10.
    """
    # Short-circuit: caller fully specified.
    if plan_type and plan_start_date:
        return plan_type, plan_start_date

    print(f"Resolving plan identity against '{LOCKED_WORKSHEET_NAME}'...")
    grid = read_worksheet(LOCKED_WORKSHEET_NAME, render="formula")
    if not grid or len(grid) < 2:
        _die(f"'{LOCKED_WORKSHEET_NAME}' has no data rows.")

    try:
        counts = _scan_plans_in_grid(grid)
    except ValidationError as e:
        _die(str(e))
        raise  # unreachable

    if not counts:
        _die(
            f"No rows with both plan_type and plan_start_date found in "
            f"'{LOCKED_WORKSHEET_NAME}'. Is the tab empty or missing those columns?"
        )

    def _inventory(filter_pt: Optional[str] = None) -> str:
        pairs = [(pt, psd, n) for (pt, psd), n in counts.items()
                 if filter_pt is None or pt == filter_pt]
        return "\n  ".join(f"{pt} / {psd}  ({n} rows)" for pt, psd, n in sorted(pairs))

    # Case 2: --plan-type given, pick newest date for that type.
    if plan_type and not plan_start_date:
        matching = [(pt, psd) for (pt, psd) in counts if pt == plan_type]
        if not matching:
            _die(
                f"No plans with plan_type='{plan_type}' in "
                f"'{LOCKED_WORKSHEET_NAME}'. Plans present:\n  {_inventory()}"
            )
        # plan_start_date is ISO YYYY-MM-DD, so string max == date max.
        newest_psd = max(psd for (_, psd) in matching)
        if len(matching) > 1:
            print(
                f"  {len(matching)} plans for plan_type='{plan_type}' in the tab; "
                f"picking newest plan_start_date={newest_psd}. To lock an older "
                f"vintage, pass --plan-start-date explicitly. Available:\n  "
                f"{_inventory(plan_type)}"
            )
        else:
            print(f"  Resolved: plan_type={plan_type}, plan_start_date={newest_psd}.")
        return plan_type, newest_psd

    # Case 3: --plan-start-date given, --plan-type omitted.
    if plan_start_date and not plan_type:
        matching = [(pt, psd) for (pt, psd) in counts if psd == plan_start_date]
        if not matching:
            _die(
                f"No plans with plan_start_date='{plan_start_date}' in "
                f"'{LOCKED_WORKSHEET_NAME}'. Plans present:\n  {_inventory()}"
            )
        if len(matching) > 1:
            _die(
                f"Multiple plan_types share plan_start_date='{plan_start_date}' "
                f"(e.g. {sorted({pt for pt, _ in matching})}). Pass --plan-type "
                f"to disambiguate."
            )
        pt = matching[0][0]
        print(f"  Resolved: plan_type={pt}, plan_start_date={plan_start_date}.")
        return pt, plan_start_date

    # Case 4: nothing given, tab has many plans → fail with the list.
    _die(
        f"'{LOCKED_WORKSHEET_NAME}' has {len(counts)} plans stacked; specify "
        f"--plan-type (required) and optionally --plan-start-date.\n"
        f"Plans in the tab:\n  {_inventory()}"
    )
    raise  # unreachable


# ── STEP 1: READ + VALIDATE THE SHEET TAB ────────────────────────────

def read_sheet_rows(
    plan_type: str,
    plan_start_date: str,
    *,
    strict: bool = False,
) -> list[VintageRow]:
    """Read the locked Sheet tab and return only rows matching this plan.

    The tab is a multi-plan archive (many plan_type × plan_start_date
    combinations stacked). We filter down to the requested plan before
    validating + returning.
    """
    print(f"Reading '{LOCKED_WORKSHEET_NAME}' from Google Sheets...")
    # FORMULA render avoids the live-model recalc timeout. It returns date
    # cells as their numeric serial (e.g. 46089), which parse_sheet_date()
    # converts back to ISO. Raw formulas in DATA rows (if any ever appear)
    # would fail to parse — switch to render='unformatted' at that point.
    grid = read_worksheet(LOCKED_WORKSHEET_NAME, render="formula")

    if not grid or len(grid) < 2:
        _die(f"'{LOCKED_WORKSHEET_NAME}' has no data rows (need header + >=1 row).")

    try:
        header_idx = find_header_row(grid, REQUIRED_COLUMNS)
    except ValidationError as e:
        _die(str(e))
        raise  # unreachable, for type checker

    header = apply_aliases(normalize_header(grid[header_idx]))
    col_idx = {name: header.index(name) for name in REQUIRED_COLUMNS}

    rows: list[VintageRow] = []
    available_plans: set[tuple[str, str]] = set()
    total_scanned = 0

    for i, raw_row in enumerate(grid[header_idx + 1 :], start=header_idx + 2):
        if not any(str(c).strip() for c in raw_row):
            continue  # skip blank rows
        total_scanned += 1

        def cell(name: str) -> str:
            idx = col_idx[name]
            return str(raw_row[idx]).strip() if idx < len(raw_row) else ""

        row_plan_type = cell("plan_type")
        row_plan_start_raw = cell("plan_start_date")
        try:
            row_plan_start = parse_sheet_date(row_plan_start_raw) if row_plan_start_raw else ""
        except ValueError:
            row_plan_start = row_plan_start_raw  # surface as-is if malformed

        # Track for the "available plans" error message.
        if row_plan_type and row_plan_start:
            available_plans.add((row_plan_type, row_plan_start))

        # Skip rows that aren't the plan we're memorializing.
        if row_plan_type != plan_type or row_plan_start != plan_start_date:
            continue

        try:
            forecast_date = parse_sheet_date(cell("forecast_date"))
            leads = parse_float_maybe(cell("leads"))
            ad_spend = parse_float_maybe(cell("ad_spend"))
        except ValueError as e:
            _die(f"Row {i}: {e}")
            raise  # unreachable, for type checker

        rows.append(
            VintageRow(
                forecast_date=forecast_date,
                business=cell("business"),
                lead_source=cell("lead_source"),
                audience=cell("audience"),
                leads=leads,
                ad_spend=ad_spend,
            )
        )

    print(
        f"  Scanned {total_scanned} rows; matched {len(rows)} for "
        f"plan_type={plan_type}, plan_start_date={plan_start_date} "
        f"(header on row {header_idx + 1})."
    )

    if not rows:
        nice_list = "\n  ".join(
            f"{pt} / {pd}" for pt, pd in sorted(available_plans)
        )
        _die(
            f"No rows in '{LOCKED_WORKSHEET_NAME}' match "
            f"plan_type='{plan_type}' and plan_start_date='{plan_start_date}'.\n"
            f"Available plans in the sheet:\n  {nice_list}"
        )

    # Structural + range + known-value validation (see vintage_validation.py).
    try:
        validate_rows([asdict(r) for r in rows], strict=strict)
    except ValidationError as e:
        _die(f"Validation failed: {e}")

    return rows


# ── STEP 2: QUERY EXISTING CURRENT VINTAGE FOR THIS PLAN ─────────────

_PAGE_SIZE = 1000  # PostgREST hard-caps list responses at ~1000 per request.


def fetch_current_vintage(plan_type: str, plan_start_date: str) -> list[dict[str, Any]]:
    """Return all current (is_current=TRUE) rows for this plan, paginated.

    Paginates because PostgREST caps list responses at ~1000 rows/request;
    an SOP vintage is ~25k rows so we'd miss 96% of them without this.
    """
    all_rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        resp = httpx.get(
            f"{SUPABASE_URL}/rest/v1/{VINTAGES_TABLE}",
            headers=_supabase_headers(),
            params={
                "select": "id",
                "plan_type": f"eq.{plan_type}",
                "plan_start_date": f"eq.{plan_start_date}",
                "is_current": "eq.true",
                "limit": _PAGE_SIZE,
                "offset": offset,
            },
            timeout=60,
        )
        if resp.status_code >= 400:
            _die(f"Fetching existing vintage: HTTP {resp.status_code} — {resp.text}")
        batch = resp.json()
        if not batch:
            break
        all_rows.extend(batch)
        if len(batch) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return all_rows


# ── STEP 3: UPSERT LOGIC ─────────────────────────────────────────────

def insert_vintage_rows(
    rows: list[VintageRow],
    plan_type: str,
    plan_start_date: str,
    locked_by: str,
    notes: Optional[str],
) -> list[dict[str, Any]]:
    """Insert new current rows; returns the inserted records (with ids)."""
    payload = [
        {
            "plan_type": plan_type,
            "plan_start_date": plan_start_date,
            "forecast_date": r.forecast_date,
            "business": r.business,
            "lead_source": r.lead_source,
            "audience": r.audience,
            "leads": r.leads,
            "ad_spend": r.ad_spend,
            "locked_by": locked_by,
            "notes": notes,
            # plan_locked_at, is_current, id all defaulted by the DB
        }
        for r in rows
    ]

    resp = httpx.post(
        f"{SUPABASE_URL}/rest/v1/{VINTAGES_TABLE}",
        headers=_supabase_headers(prefer_return=True),
        json=payload,
        timeout=120,
    )
    if resp.status_code >= 400:
        _die(f"Insert failed: HTTP {resp.status_code} — {resp.text}")
    inserted = resp.json()
    print(f"  Inserted {len(inserted)} new rows.")
    return inserted


def supersede_rows(old_ids: list[int], supersede_reason: str) -> None:
    """Flip old rows to is_current=FALSE with reason; leaves superseded_by NULL (patched later)."""
    if not old_ids:
        return
    payload = {
        "is_current": False,
        "superseded_at": "now()",
        "supersede_reason": supersede_reason,
    }
    resp = httpx.patch(
        f"{SUPABASE_URL}/rest/v1/{VINTAGES_TABLE}",
        headers=_supabase_headers(),
        params={"id": f"in.({','.join(str(i) for i in old_ids)})"},
        json=payload,
        timeout=60,
    )
    if resp.status_code >= 400:
        _die(f"Supersede PATCH failed: HTTP {resp.status_code} — {resp.text}")
    print(f"  Flipped {len(old_ids)} old rows to is_current=FALSE.")


def patch_superseded_by(old_ids: list[int], new_id_placeholder: int) -> None:
    """Point every superseded row at a representative replacement id.

    We don't attempt per-row pairing (that requires joining on the full
    business-key combo, which is fragile if rows were added or dropped between
    locks). A single pointer is enough for audit purposes.
    """
    if not old_ids:
        return
    resp = httpx.patch(
        f"{SUPABASE_URL}/rest/v1/{VINTAGES_TABLE}",
        headers=_supabase_headers(),
        params={"id": f"in.({','.join(str(i) for i in old_ids)})"},
        json={"superseded_by": new_id_placeholder},
        timeout=60,
    )
    if resp.status_code >= 400:
        _die(f"superseded_by PATCH failed: HTTP {resp.status_code} — {resp.text}")
    print(f"  Pointed {len(old_ids)} superseded rows at replacement id {new_id_placeholder}.")


# ── MAIN ─────────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # Normal weekly use: pass --plan-type only; --plan-start-date is picked as
    # the newest value in the tab for that plan_type. Pass both to pin a
    # specific historical vintage. Pass neither to get a listing of plans in
    # the tab (useful one-liner for "what's in there?" spot-checks).
    p.add_argument(
        "--plan-type",
        default=None,
        help="e.g. SOP, OP, SOPM. Omit to list available plans in the tab.",
    )
    p.add_argument(
        "--plan-start-date",
        default=None,
        help="The Sunday the plan goes into effect (YYYY-MM-DD). Omit to pick "
        "the newest plan_start_date for --plan-type in the tab.",
    )
    p.add_argument("--locked-by", required=True, help="e.g. christian.thomas")
    p.add_argument("--notes", default=None, help="Freeform notes (optional)")
    p.add_argument(
        "--supersede-reason",
        default=None,
        help="Required if this is a re-lock. Short human-readable reason.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Read + validate the Sheet but do not write to Supabase.",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Treat unknown business/lead_source/audience values as errors (default: warn).",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    _validate_env()

    # Step 0: resolve plan identity (no-op if both flags given; tab scan otherwise)
    plan_type, plan_start_date = resolve_plan_identity(
        args.plan_type, args.plan_start_date
    )

    try:
        parse_date_str(plan_start_date, "--plan-start-date")
    except ValueError as e:
        _die(str(e))

    # Step 1: read + validate the Sheet (filters to the target plan)
    rows = read_sheet_rows(
        plan_type=plan_type,
        plan_start_date=plan_start_date,
        strict=args.strict,
    )

    # Step 2: detect re-lock
    existing = fetch_current_vintage(plan_type, plan_start_date)
    is_relock = len(existing) > 0
    if is_relock:
        if not args.supersede_reason:
            _die(
                f"Plan {plan_type} / {plan_start_date} already has "
                f"{len(existing)} current rows in {VINTAGES_TABLE}. "
                f"Pass --supersede-reason '<why>' to confirm this is an intentional re-lock."
            )
        print(
            f"Re-lock detected: {len(existing)} existing current rows will be "
            f"superseded with reason: {args.supersede_reason}"
        )
    else:
        print(f"New lock for {plan_type} / {plan_start_date}.")

    if args.dry_run:
        print(f"[DRY RUN] Would write {len(rows)} rows. Exiting without changes.")
        return 0

    # Step 3a: insert new rows FIRST so we have replacement ids for superseded_by
    inserted = insert_vintage_rows(
        rows,
        plan_type=plan_type,
        plan_start_date=plan_start_date,
        locked_by=args.locked_by,
        notes=args.notes,
    )

    # Step 3b: if re-lock, flip old rows and point them at the new ones
    if is_relock:
        old_ids = [r["id"] for r in existing]
        supersede_rows(old_ids, args.supersede_reason)
        # Use the smallest new id as the canonical replacement pointer.
        # (Any of the new ids would do; this is stable and easy to verify.)
        replacement_id = min(r["id"] for r in inserted)
        patch_superseded_by(old_ids, replacement_id)

    print(
        f"\nMemorialized {len(inserted)} rows for "
        f"plan_type={plan_type}, plan_start_date={plan_start_date}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
