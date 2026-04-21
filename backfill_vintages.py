"""One-shot backfill of every plan currently sitting in the locked archive tab.

Reads the archive tab (default: "Daily Forecast - LOCKED For Summary Tables")
once, groups its rows by (plan_type, plan_start_date), and memorializes each
group into Supabase. Plans that already have a current vintage are skipped so
the script is idempotent — safe to re-run after a partial failure.

Note on tab resolution: memorialize_forecast.py's default LOCKED_WORKSHEET_NAME
now points at the Pattern-A current-plan-only tab ("Current Plan - LOCKED")
because that's the going-forward weekly ritual. Backfill intentionally reads
the OLD multi-plan archive, so it has its own tab name (overridable via
ARCHIVE_WORKSHEET_NAME env var).

Usage
-----
    python backfill_vintages.py --locked-by christian.thomas

    # Dry run (print what would be written, no Supabase writes):
    python backfill_vintages.py --locked-by christian.thomas --dry-run

    # Force re-memorialization of plans that already exist (treat as re-locks):
    python backfill_vintages.py --locked-by christian.thomas \
        --force --supersede-reason "One-time backfill re-run"

Design
------
This is intentionally a separate entry point from memorialize_forecast.py so
it can't be confused with the weekly ritual. It exists for the Phase 1
backfill and will be deleted (or archived) once the archive is complete.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from dataclasses import asdict
from typing import Optional

from dotenv import load_dotenv

from memorialize_forecast import (
    REQUIRED_COLUMNS,
    VintageRow,
    _supabase_headers,
    _validate_env,
    fetch_current_vintage,
    insert_vintage_rows,
    patch_superseded_by,
    supersede_rows,
)

# The archive tab we're backfilling FROM. Not the same as memorialize's
# LOCKED_WORKSHEET_NAME (which after the Pattern-A switch points at the
# current-plan-only tab).
ARCHIVE_WORKSHEET_NAME = os.environ.get(
    "ARCHIVE_WORKSHEET_NAME", "Daily Forecast - LOCKED For Summary Tables"
)
from read_from_sheets import read_worksheet
from vintage_validation import (
    ValidationError,
    apply_aliases,
    find_header_row,
    normalize_header,
    parse_float_maybe,
    parse_sheet_date,
    validate_rows,
)

load_dotenv()


def read_all_plans() -> dict[tuple[str, str], list[VintageRow]]:
    """Return {(plan_type, plan_start_date): [VintageRow, ...]} for every plan."""
    print(f"Reading '{ARCHIVE_WORKSHEET_NAME}' from Google Sheets...")
    grid = read_worksheet(ARCHIVE_WORKSHEET_NAME, render="formula")
    if not grid or len(grid) < 2:
        print(f"ERROR: '{ARCHIVE_WORKSHEET_NAME}' has no data rows.", file=sys.stderr)
        sys.exit(2)

    header_idx = find_header_row(grid, REQUIRED_COLUMNS)
    header = apply_aliases(normalize_header(grid[header_idx]))
    col_idx = {name: header.index(name) for name in REQUIRED_COLUMNS}

    grouped: dict[tuple[str, str], list[VintageRow]] = defaultdict(list)
    bad_rows = 0
    for i, raw in enumerate(grid[header_idx + 1 :], start=header_idx + 2):
        if not any(str(c).strip() for c in raw):
            continue

        def cell(name: str) -> str:
            idx = col_idx[name]
            return str(raw[idx]).strip() if idx < len(raw) else ""

        plan_type = cell("plan_type")
        plan_start_raw = cell("plan_start_date")
        if not plan_type or not plan_start_raw:
            bad_rows += 1
            continue

        try:
            plan_start = parse_sheet_date(plan_start_raw)
            forecast_date = parse_sheet_date(cell("forecast_date"))
            leads = parse_float_maybe(cell("leads"))
            ad_spend = parse_float_maybe(cell("ad_spend"))
        except ValueError as e:
            print(f"WARNING: row {i} skipped — {e}", file=sys.stderr)
            bad_rows += 1
            continue

        grouped[(plan_type, plan_start)].append(
            VintageRow(
                forecast_date=forecast_date,
                business=cell("business"),
                lead_source=cell("lead_source"),
                audience=cell("audience"),
                leads=leads,
                ad_spend=ad_spend,
            )
        )

    total = sum(len(rs) for rs in grouped.values())
    print(
        f"  Grouped into {len(grouped)} plans ({total} data rows"
        + (f", {bad_rows} malformed rows skipped)." if bad_rows else ").")
    )
    return grouped


def process_plan(
    plan_type: str,
    plan_start_date: str,
    rows: list[VintageRow],
    locked_by: str,
    notes: Optional[str],
    force: bool,
    supersede_reason: Optional[str],
    dry_run: bool,
    strict: bool,
) -> str:
    """Return a one-word status: 'inserted', 'skipped', 'resupersede', 'dry'."""
    tag = f"{plan_type} / {plan_start_date}"

    # Validate. Negatives are allowed (historical correction rows); everything
    # structural still hard-fails.
    try:
        validate_rows(
            [asdict(r) for r in rows],
            strict=strict,
            allow_negative_metrics=True,
        )
    except ValidationError as e:
        print(f"[{tag}] FAILED validation: {e}", file=sys.stderr)
        sys.exit(3)

    existing = fetch_current_vintage(plan_type, plan_start_date)
    already_memorialized = len(existing) > 0

    if already_memorialized and not force:
        print(f"[{tag}] SKIP — already memorialized ({len(existing)} current rows).")
        return "skipped"

    if dry_run:
        verb = "re-write" if already_memorialized else "insert"
        print(f"[{tag}] DRY RUN — would {verb} {len(rows)} rows.")
        return "dry"

    inserted = insert_vintage_rows(
        rows,
        plan_type=plan_type,
        plan_start_date=plan_start_date,
        locked_by=locked_by,
        notes=notes,
    )

    if already_memorialized:
        old_ids = [r["id"] for r in existing]
        supersede_rows(old_ids, supersede_reason or "backfill re-run")
        replacement_id = min(r["id"] for r in inserted)
        patch_superseded_by(old_ids, replacement_id)
        return "resupersede"

    return "inserted"


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--locked-by", required=True)
    p.add_argument("--notes", default="Historical backfill of Sheet archive")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--strict", action="store_true")
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-memorialize plans that already exist (treat as re-locks).",
    )
    p.add_argument(
        "--supersede-reason",
        default=None,
        help="Required with --force. Short human-readable reason.",
    )
    args = p.parse_args(argv)

    _validate_env()
    if args.force and not args.supersede_reason:
        print("ERROR: --force requires --supersede-reason.", file=sys.stderr)
        return 2

    grouped = read_all_plans()

    summary: dict[str, int] = defaultdict(int)
    for (pt, psd), rows in sorted(grouped.items()):
        status = process_plan(
            plan_type=pt,
            plan_start_date=psd,
            rows=rows,
            locked_by=args.locked_by,
            notes=args.notes,
            force=args.force,
            supersede_reason=args.supersede_reason,
            dry_run=args.dry_run,
            strict=args.strict,
        )
        summary[status] += 1

    print("\n-- SUMMARY " + "-" * 30)
    for k in ("inserted", "resupersede", "skipped", "dry"):
        if summary[k]:
            print(f"  {k:12} {summary[k]} plan(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
