"""Compute eLTV seasonal index from marketing_eltv_actuals.

For each BU portfolio, computes:
  seasonal_index[iso_week] = mean(eLTV for that iso_week across years) / grand_mean

Also computes a trailing average for the most recent complete weeks,
which serves as the "current level" baseline for forecasting.

Output: eltv_seasonal_index.csv
  Columns: business, iso_week, avg_eltv, seasonal_index, n_years

Usage:
    python compute_eltv_seasonal.py
    python compute_eltv_seasonal.py --trailing-weeks 13
"""
import os, csv, argparse
from datetime import date, datetime, timedelta
from collections import defaultdict

import httpx
from dotenv import load_dotenv

load_dotenv()

URL = os.environ["SUPABASE_URL"]
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
HDR = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}

OUT_DIR = os.path.dirname(os.path.abspath(__file__))


def fetch_eltv():
    """Fetch all eLTV actuals from Supabase."""
    rows = []
    offset = 0
    while True:
        r = httpx.get(f"{URL}/rest/v1/marketing_eltv_actuals", headers=HDR, params={
            "select": "week_start,business,eltv_per_client,client_count,conv_value",
            "order": "week_start.asc",
            "limit": "1000",
            "offset": str(offset),
        }, timeout=30)
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
        if len(batch) < 1000:
            break
    print(f"  Fetched {len(rows)} rows from marketing_eltv_actuals")
    return rows


def compute_seasonal_index(rows, trailing_weeks=13):
    """Compute seasonal index per BU.

    Returns dict: {bu: {iso_week: {avg_eltv, seasonal_index, n_years, years}}}
    Also returns trailing averages per BU.
    """
    # Group by (bu, iso_week, year)
    by_bu = defaultdict(list)
    by_bu_all = defaultdict(list)
    by_bu_recent = defaultdict(list)

    # Find the most recent complete week (skip partial current week)
    all_weeks = sorted(set(r["week_start"] for r in rows))
    today = date.today()
    # Current week's Sunday
    current_week_start = today - timedelta(days=today.weekday() + 1)
    if current_week_start > today:
        current_week_start -= timedelta(days=7)
    current_week_str = current_week_start.strftime("%Y-%m-%d")

    print(f"  Current (possibly partial) week: {current_week_str}")
    complete_weeks = [w for w in all_weeks if w < current_week_str]
    print(f"  Complete weeks: {len(complete_weeks)} (excluding partial)")

    for r in rows:
        bu = r["business"]
        eltv = r["eltv_per_client"]
        week_str = r["week_start"]
        if eltv is None:
            continue

        eltv = float(eltv)
        dt = datetime.strptime(week_str, "%Y-%m-%d").date()
        iso_week = dt.isocalendar()[1]
        year = dt.year

        by_bu[(bu, iso_week)].append({"eltv": eltv, "year": year})
        by_bu_all[bu].append(eltv)

        if week_str in complete_weeks[-trailing_weeks:]:
            by_bu_recent[bu].append(eltv)

    results = {}
    trailing_avgs = {}

    for bu in sorted(set(b for (b, _) in by_bu.keys())):
        grand_mean = sum(by_bu_all[bu]) / len(by_bu_all[bu]) if by_bu_all[bu] else 1
        trailing_avg = sum(by_bu_recent[bu]) / len(by_bu_recent[bu]) if by_bu_recent[bu] else grand_mean

        results[bu] = {}
        for iso_week in range(1, 54):
            entries = by_bu.get((bu, iso_week), [])
            if not entries:
                continue
            avg_eltv = sum(e["eltv"] for e in entries) / len(entries)
            years = sorted(set(e["year"] for e in entries))
            results[bu][iso_week] = {
                "avg_eltv": round(avg_eltv, 2),
                "seasonal_index": round(avg_eltv / grand_mean, 4),
                "n_years": len(entries),
                "years": years,
            }

        trailing_avgs[bu] = {
            "grand_mean": round(grand_mean, 2),
            "trailing_avg": round(trailing_avg, 2),
            "trailing_weeks": len(by_bu_recent[bu]),
        }

    return results, trailing_avgs


def write_csv(results, trailing_avgs, out_path):
    """Write seasonal index CSV."""
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["business", "iso_week", "avg_eltv", "seasonal_index", "n_years", "years"])
        for bu in sorted(results.keys()):
            for iso_week in sorted(results[bu].keys()):
                d = results[bu][iso_week]
                w.writerow([
                    bu,
                    iso_week,
                    d["avg_eltv"],
                    d["seasonal_index"],
                    d["n_years"],
                    "|".join(str(y) for y in d["years"]),
                ])
    print(f"  Wrote {out_path}")


def print_summary(results, trailing_avgs):
    for bu in sorted(results.keys()):
        ta = trailing_avgs[bu]
        print(f"\n  {bu}:")
        print(f"    Grand mean eLTV:      ${ta['grand_mean']:,.2f}")
        print(f"    Trailing {ta['trailing_weeks']}-wk avg:   ${ta['trailing_avg']:,.2f}")
        print(f"    ISO weeks with data:  {len(results[bu])}")

        # Show range of seasonal indices
        indices = [d["seasonal_index"] for d in results[bu].values()]
        if indices:
            print(f"    Seasonal index range: {min(indices):.4f} - {max(indices):.4f}")

        # Show a few interesting weeks
        print(f"    Sample weeks:")
        for wk in [1, 10, 20, 30, 40, 47, 52]:
            d = results[bu].get(wk)
            if d:
                print(f"      Wk {wk:>2}: avg=${d['avg_eltv']:>8,.2f}  idx={d['seasonal_index']:.4f}  "
                      f"({d['n_years']} yr{'s' if d['n_years'] > 1 else ''})")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trailing-weeks", type=int, default=13)
    args = parser.parse_args()

    print("Fetching eLTV data from Supabase...")
    rows = fetch_eltv()

    print(f"\nComputing seasonal index (trailing={args.trailing_weeks} weeks)...")
    results, trailing_avgs = compute_seasonal_index(rows, trailing_weeks=args.trailing_weeks)

    out_path = os.path.join(OUT_DIR, "eltv_seasonal_index.csv")
    write_csv(results, trailing_avgs, out_path)

    print_summary(results, trailing_avgs)

    # Also write trailing averages as a separate small file for easy reference
    ta_path = os.path.join(OUT_DIR, "eltv_trailing_avg.csv")
    with open(ta_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["business", "grand_mean", "trailing_avg", "trailing_weeks"])
        for bu in sorted(trailing_avgs.keys()):
            ta = trailing_avgs[bu]
            w.writerow([bu, ta["grand_mean"], ta["trailing_avg"], ta["trailing_weeks"]])
    print(f"\n  Wrote {ta_path}")
    print("\nDone.")


if __name__ == "__main__":
    main()
