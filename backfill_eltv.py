"""Backfill marketing_eltv_actuals from a multi-BU Looker CSV export.

The CSV has rows per (week, business) with columns:
  Record Created Week, Business, Count, Conv Value

Processing:
  - VT Core + International are summed into a combined portfolio eLTV,
    then inserted as separate rows with identical values (shared bidding portfolio).
  - Prof Certs is kept as its own row.
  - "Unallocated" rows are ignored.
  - The most recent week is skipped (partial week).

Usage:
    python backfill_eltv.py [--dry-run]
    python backfill_eltv.py --csv path/to/file.csv
    python backfill_eltv.py --skip-weeks 2026-04-19
"""
import os, csv, re, argparse
from datetime import date
from collections import defaultdict

import httpx
from dotenv import load_dotenv

load_dotenv()

URL = os.environ["SUPABASE_URL"]
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
HDR = {
    "apikey": KEY,
    "Authorization": f"Bearer {KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}

DEFAULT_CSV = os.path.join(
    os.environ.get("USERPROFILE", os.environ.get("HOME", ".")),
    "Downloads",
    "Marketing Model - eLTV per Client 2026-04-24T1216.csv",
)

COMBINED_BUS = {"VT Core", "International"}
STANDALONE_BUS = {"Prof Certs"}
IGNORE_BUS = {"Unallocated"}

SOURCE = "backfill:multi_bu_csv_2026-04-24"


def _parse_money(val):
    if not val:
        return 0.0
    return float(re.sub(r"[$,\"]", "", str(val)))


def _parse_int(val):
    if not val:
        return 0
    return int(re.sub(r"[,\"]", "", str(val)))


def load_csv(path, skip_weeks=None):
    """Load CSV and return list of insert-ready rows.

    Returns list of dicts with keys: week_start, business, client_count, conv_value
    """
    skip = set(skip_weeks or [])

    # First pass: accumulate raw data by (week, bu)
    raw = defaultdict(lambda: {"count": 0, "value": 0.0})
    all_weeks = set()

    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            week = r.get("Record Created Week", "").strip()
            bu = r.get("Business", "").strip()
            if not week or not bu:
                continue
            all_weeks.add(week)
            raw[(week, bu)]["count"] += _parse_int(r.get("Count"))
            raw[(week, bu)]["value"] += _parse_money(r.get("Conv Value"))

    # Auto-skip the most recent week (partial)
    most_recent = max(all_weeks) if all_weeks else None
    if most_recent:
        skip.add(most_recent)
        print(f"  Auto-skipping most recent (partial) week: {most_recent}")

    for s in skip:
        print(f"  Skipping week: {s}")

    # Second pass: build insert rows
    weeks = sorted(all_weeks - skip)
    rows = []

    for week in weeks:
        # Combined portfolio (VT Core + International)
        combined_count = 0
        combined_value = 0.0
        for bu in COMBINED_BUS:
            d = raw.get((week, bu))
            if d:
                combined_count += d["count"]
                combined_value += d["value"]

        if combined_count > 0:
            for bu in ["VT Core", "International"]:
                rows.append({
                    "week_start": week,
                    "business": bu,
                    "client_count": combined_count,
                    "conv_value": round(combined_value, 2),
                })

        # Prof Certs (standalone)
        for bu in STANDALONE_BUS:
            d = raw.get((week, bu))
            if d and d["count"] > 0:
                rows.append({
                    "week_start": week,
                    "business": bu,
                    "client_count": d["count"],
                    "conv_value": round(d["value"], 2),
                })

    print(f"  {len(weeks)} complete weeks, {len(rows)} rows to insert")
    return rows


def wipe_existing():
    """Delete all existing rows from marketing_eltv_actuals."""
    print("Wiping existing rows...")
    resp = httpx.delete(
        f"{URL}/rest/v1/marketing_eltv_actuals",
        headers={**HDR, "Prefer": "return=minimal"},
        params={"id": "gt.0"},
        timeout=30,
    )
    if resp.status_code in (200, 204):
        print("  Wiped.")
    else:
        print(f"  WARNING: wipe returned {resp.status_code}: {resp.text}")


def backfill(rows, dry_run=False):
    total = 0
    errors = 0

    for r in rows:
        eltv = round(r["conv_value"] / r["client_count"], 2) if r["client_count"] > 0 else None

        if dry_run:
            print(f"  [DRY] {r['week_start']} | {r['business']:15s} | "
                  f"clients={r['client_count']:>5} | "
                  f"conv=${r['conv_value']:>12,.2f} | "
                  f"eLTV=${eltv:>8,.2f}" if eltv else
                  f"  [DRY] {r['week_start']} | {r['business']:15s} | "
                  f"clients={r['client_count']:>5} | "
                  f"conv=${r['conv_value']:>12,.2f} | eLTV=n/a")
            total += 1
            continue

        payload = {
            "week_start": r["week_start"],
            "business": r["business"],
            "client_count": r["client_count"],
            "conv_value": r["conv_value"],
            "source": SOURCE,
        }

        resp = httpx.post(
            f"{URL}/rest/v1/marketing_eltv_actuals",
            headers={**HDR, "Prefer": "return=minimal,resolution=merge-duplicates"},
            json=payload,
            timeout=30,
        )

        if resp.status_code in (200, 201):
            total += 1
        else:
            print(f"  ERROR {resp.status_code} for {r['week_start']} {r['business']}: {resp.text}")
            errors += 1

    print(f"\n  Inserted: {total}  |  Errors: {errors}")


def main():
    parser = argparse.ArgumentParser(description="Backfill marketing_eltv_actuals")
    parser.add_argument("--csv", default=DEFAULT_CSV, help="Path to CSV file")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-weeks", nargs="*", default=[], help="Additional weeks to skip")
    parser.add_argument("--no-wipe", action="store_true", help="Don't wipe existing data first")
    args = parser.parse_args()

    print(f"Loading CSV: {args.csv}")
    rows = load_csv(args.csv, skip_weeks=args.skip_weeks)

    if not rows:
        print("No rows to insert.")
        return

    if not args.dry_run and not args.no_wipe:
        wipe_existing()

    print(f"\nBackfilling {len(rows)} rows...")
    backfill(rows, dry_run=args.dry_run)
    print("\nDone.")


if __name__ == "__main__":
    main()
