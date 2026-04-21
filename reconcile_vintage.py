"""Reconcile a memorialized vintage in Supabase against the source Sheet tab.

Pulls the CURRENT (is_current=TRUE) rows for a given plan from
marketing_forecast_vintages_current, pulls the live contents of the locked
Sheet tab, and diffs them grain-by-grain. Exits 0 if they match exactly, 1 if
any drift is found.

Intended use during the 2-4 week shadow period: run this shortly after
memorialize_forecast.py to confirm the write round-tripped cleanly, and run it
periodically afterwards to catch Sheet drift (someone edited the locked tab
after lock time, etc.).

Prerequisites
-------------
Same env vars as memorialize_forecast.py:
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
    GOOGLE_CREDS_FILE, SPREADSHEET_NAME, LOCKED_WORKSHEET_NAME

Usage
-----
Weekly happy path — reconcile the newest vintage of a plan_type:

    python reconcile_vintage.py --plan-type SOP

Pin a specific vintage:

    python reconcile_vintage.py --plan-type SOP --plan-start-date 2026-04-26

    # Show the first N differences (default 25) when there are many:
    python reconcile_vintage.py --plan-type SOP --plan-start-date 2026-04-26 --max-diffs 50

Exit codes
----------
    0  vintage matches the Sheet
    1  drift found (details printed to stdout)
    2+ environment / fetch error
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
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
)
# resolve_plan_identity() lives in memorialize so both scripts agree on what
# counts as "the plan in the tab we're operating on". Importing it keeps the
# auto-pick-newest-date logic in one place.
from memorialize_forecast import resolve_plan_identity

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
# Same default as memorialize_forecast.py — the multi-plan paste-values
# archive. Filtering to the plan we're reconciling happens in fetch_sheet_rows.
LOCKED_WORKSHEET_NAME = os.environ.get(
    "LOCKED_WORKSHEET_NAME", "Daily Forecast - LOCKED For Summary Tables"
)

# The current-rows view in Supabase. Keeps reconcile pointed at a single
# identifier so rename migrations only touch one place.
VINTAGES_CURRENT_VIEW = "marketing_forecast_vintages_current"

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

# Grain columns identify a unique row; metric columns are compared for equality.
GRAIN = ("forecast_date", "business", "lead_source", "audience")
METRICS = ("leads", "ad_spend")

# Floats rarely match to the last digit after a round-trip through a Sheet,
# CSV, or JSON. 1e-6 tolerance is tight enough to catch real drift but loose
# enough to ignore storage noise (NUMERIC <-> float64 <-> text).
FLOAT_TOLERANCE = 1e-6


# ── HELPERS ──────────────────────────────────────────────────────────

def _die(msg: str, code: int = 2) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _validate_env() -> None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        _die("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in env/.env")


def _parse_float_silent(raw: Any) -> Optional[float]:
    """Reconcile-side parse: never raise; return None on anything unparseable.

    Different from write-path parsing: we tolerate junk in the Sheet here
    because the goal is to report drift, not block on it.
    """
    try:
        return parse_float_maybe(raw)
    except ValueError:
        return None


def _floats_equal(a: Optional[float], b: Optional[float]) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= FLOAT_TOLERANCE


# ── SUPABASE SIDE ────────────────────────────────────────────────────

_PAGE_SIZE = 1000  # PostgREST hard-caps list responses at ~1000 per request.


def fetch_supabase_vintage(plan_type: str, plan_start_date: str) -> list[dict[str, Any]]:
    """Pull all current (is_current=TRUE) rows for this plan via the view.

    Supabase/PostgREST caps list responses at 1000 rows by default, so we
    paginate with offset/limit until we get a short page.
    """
    all_rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        resp = httpx.get(
            f"{SUPABASE_URL}/rest/v1/{VINTAGES_CURRENT_VIEW}",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
            },
            params={
                "select": ",".join(GRAIN + METRICS),
                "plan_type": f"eq.{plan_type}",
                "plan_start_date": f"eq.{plan_start_date}",
                "limit": _PAGE_SIZE,
                "offset": offset,
            },
            timeout=60,
        )
        if resp.status_code >= 400:
            _die(f"Fetching vintage from Supabase: HTTP {resp.status_code} — {resp.text}")
        batch = resp.json()
        if not batch:
            break
        all_rows.extend(batch)
        if len(batch) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return all_rows


# ── SHEET SIDE ───────────────────────────────────────────────────────

def _parse_sheet_date_silent(raw: Any) -> str:
    """Reconcile-side date parse: never raise; return raw stringified on failure.

    If the Sheet has a malformed date, we want to surface it as an "only in
    Sheet" row in the diff (so the user sees it), not crash the reconciler.
    """
    try:
        return parse_sheet_date(raw)
    except ValueError:
        return str(raw).strip()


def fetch_sheet_rows(plan_type: str, plan_start_date: str) -> list[dict[str, Any]]:
    """Read the locked Sheet tab and filter to just the target plan."""
    grid = read_worksheet(LOCKED_WORKSHEET_NAME, render="formula")
    if not grid or len(grid) < 2:
        _die(f"'{LOCKED_WORKSHEET_NAME}' has no data rows.")

    try:
        header_idx = find_header_row(grid, REQUIRED_COLUMNS)
    except ValidationError as e:
        _die(str(e))
        raise  # unreachable

    header = apply_aliases(normalize_header(grid[header_idx]))
    col_idx = {name: header.index(name) for name in REQUIRED_COLUMNS}

    rows: list[dict[str, Any]] = []
    for raw in grid[header_idx + 1 :]:
        if not any(str(c).strip() for c in raw):
            continue
        def cell(name: str) -> str:
            idx = col_idx[name]
            return str(raw[idx]).strip() if idx < len(raw) else ""

        row_plan_type = cell("plan_type")
        row_plan_start = _parse_sheet_date_silent(cell("plan_start_date"))
        if row_plan_type != plan_type or row_plan_start != plan_start_date:
            continue

        rows.append(
            {
                "forecast_date": _parse_sheet_date_silent(cell("forecast_date")),
                "business": cell("business"),
                "lead_source": cell("lead_source"),
                "audience": cell("audience"),
                "leads": _parse_float_silent(cell("leads")),
                "ad_spend": _parse_float_silent(cell("ad_spend")),
            }
        )
    return rows


# ── DIFF ─────────────────────────────────────────────────────────────

@dataclass
class DiffReport:
    only_in_supabase: list[tuple[Any, ...]] = field(default_factory=list)
    only_in_sheet: list[tuple[Any, ...]] = field(default_factory=list)
    metric_mismatches: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not (self.only_in_supabase or self.only_in_sheet or self.metric_mismatches)

    @property
    def total_diffs(self) -> int:
        return (
            len(self.only_in_supabase)
            + len(self.only_in_sheet)
            + len(self.metric_mismatches)
        )


def diff_rows(
    supabase_rows: list[dict[str, Any]], sheet_rows: list[dict[str, Any]]
) -> DiffReport:
    def key(r: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(r[c] for c in GRAIN)

    sb_by_key = {key(r): r for r in supabase_rows}
    sh_by_key = {key(r): r for r in sheet_rows}

    sb_keys = set(sb_by_key)
    sh_keys = set(sh_by_key)

    report = DiffReport()
    report.only_in_supabase = sorted(sb_keys - sh_keys)
    report.only_in_sheet = sorted(sh_keys - sb_keys)

    for k in sorted(sb_keys & sh_keys):
        sb = sb_by_key[k]
        sh = sh_by_key[k]
        for m in METRICS:
            if not _floats_equal(_parse_float_silent(sb[m]), _parse_float_silent(sh[m])):
                report.metric_mismatches.append(
                    {
                        "key": dict(zip(GRAIN, k)),
                        "metric": m,
                        "supabase": sb[m],
                        "sheet": sh[m],
                    }
                )
    return report


def print_report(report: DiffReport, max_diffs: int) -> None:
    if report.is_clean:
        print("MATCH: Supabase vintage matches the Sheet tab exactly.")
        return

    print(f"DRIFT: {report.total_diffs} total difference(s) found.\n")

    if report.only_in_supabase:
        print(f"Rows present in Supabase but NOT in Sheet: {len(report.only_in_supabase)}")
        for k in report.only_in_supabase[:max_diffs]:
            print(f"  - {dict(zip(GRAIN, k))}")
        if len(report.only_in_supabase) > max_diffs:
            print(f"  ... ({len(report.only_in_supabase) - max_diffs} more)")
        print()

    if report.only_in_sheet:
        print(f"Rows present in Sheet but NOT in Supabase: {len(report.only_in_sheet)}")
        for k in report.only_in_sheet[:max_diffs]:
            print(f"  + {dict(zip(GRAIN, k))}")
        if len(report.only_in_sheet) > max_diffs:
            print(f"  ... ({len(report.only_in_sheet) - max_diffs} more)")
        print()

    if report.metric_mismatches:
        print(f"Metric mismatches: {len(report.metric_mismatches)}")
        for m in report.metric_mismatches[:max_diffs]:
            print(f"  ~ {m['key']} :: {m['metric']}: supabase={m['supabase']} sheet={m['sheet']}")
        if len(report.metric_mismatches) > max_diffs:
            print(f"  ... ({len(report.metric_mismatches) - max_diffs} more)")


# ── MAIN ─────────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # Same contract as memorialize: pass --plan-type and we'll pick the
    # newest plan_start_date for that type; pass both to pin; pass neither to
    # get a listing of plans in the tab.
    p.add_argument(
        "--plan-type",
        default=None,
        help="e.g. SOP, OP, SOPM. Omit to list available plans in the tab.",
    )
    p.add_argument(
        "--plan-start-date",
        default=None,
        help="YYYY-MM-DD. Omit to use the newest date for --plan-type.",
    )
    p.add_argument(
        "--max-diffs",
        type=int,
        default=25,
        help="Max number of differences to show per category (default 25).",
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    _validate_env()

    plan_type, plan_start_date = resolve_plan_identity(
        args.plan_type, args.plan_start_date
    )

    try:
        parse_date_str(plan_start_date, "--plan-start-date")
    except ValueError as e:
        _die(str(e))

    print(f"Fetching current vintage from Supabase: {plan_type} / {plan_start_date}...")
    sb_rows = fetch_supabase_vintage(plan_type, plan_start_date)
    print(f"  {len(sb_rows)} rows in Supabase (is_current=TRUE).")

    if not sb_rows:
        print("  No memorialized vintage found. Nothing to reconcile.")
        return 1

    print(f"Fetching '{LOCKED_WORKSHEET_NAME}' from Google Sheets...")
    sh_rows = fetch_sheet_rows(plan_type, plan_start_date)
    print(f"  {len(sh_rows)} rows in Sheet for this plan.\n")

    report = diff_rows(sb_rows, sh_rows)
    print_report(report, max_diffs=args.max_diffs)

    return 0 if report.is_clean else 1


if __name__ == "__main__":
    sys.exit(main())
