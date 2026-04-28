"""tROAS + eLTV target adjustment analysis (review only -- not production).

Reads planned tROAS targets from the Top Line tab (per BU, per week),
compares to prior-year same-week actual tROAS from Daily ROAS.xlsx,
fetches eLTV from marketing_eltv_actuals in Supabase, and computes:

  combined_adj = (CY_eLTV / PY_eLTV) x (PY_tROAS / CY_tROAS)

The combined factor captures the full bid-strategy picture: effective
bid per lead = eLTV / tROAS.  A CY eLTV increase at the same tROAS
means more aggressive bidding => more leads, and vice versa.

Usage:
    python analyze_troas_adjustment.py
"""
import os, re, csv
from datetime import date, timedelta, datetime
from collections import defaultdict
from typing import Optional

import httpx
import openpyxl
from dotenv import load_dotenv

from read_from_sheets import read_worksheet

load_dotenv()

ROAS_FILE = os.path.join(
    os.environ.get("USERPROFILE", ""),
    "Downloads", "Daily ROAS.xlsx",
)

EXCEL_EPOCH = date(1899, 12, 30)
BUS_OF_INTEREST = ["VT Core", "International", "Prof Certs"]

ROAS_BU_MAP = {"VT Core": "CORE", "International": "INTL", "Prof Certs": "Prof Certs"}

TRAILING_WEEKS_MIX = 13  # look-back window for paid mix calc


# ── 1. Resolve Top Line week dates ──────────────────────────────
def _serial_to_date(serial) -> Optional[date]:
    try:
        n = int(float(serial))
        if n < 40000 or n > 50000:
            return None
        return EXCEL_EPOCH + timedelta(days=n)
    except (ValueError, TypeError):
        return None


def _resolve_date_chain(rows, col_e_idx=4):
    """Walk the E-column formula chain to resolve week_start dates.

    Handles both literal serial dates and formulas like =E55+7.
    Returns {row_0idx: date}.
    """
    raw = {}
    for i, r in enumerate(rows):
        val = r[col_e_idx] if len(r) > col_e_idx else ""
        raw[i] = str(val).strip()

    resolved = {}
    for i, val in raw.items():
        d = _serial_to_date(val)
        if d:
            resolved[i] = d

    changed = True
    while changed:
        changed = False
        for i, val in raw.items():
            if i in resolved:
                continue
            m = re.match(r"^=\$?E\$?(\d+)\s*([+\-]\s*\d+)$", val, re.IGNORECASE)
            if m:
                ref_sheet_row = int(m.group(1))
                offset_days = int(m.group(2).replace(" ", ""))
                ref_idx = ref_sheet_row - 1  # 0-indexed
                if ref_idx in resolved:
                    resolved[i] = resolved[ref_idx] + timedelta(days=offset_days)
                    changed = True
                continue
            m2 = re.match(r"^=\$?E\$?(\d+)$", val, re.IGNORECASE)
            if m2:
                ref_idx = int(m2.group(1)) - 1
                if ref_idx in resolved:
                    resolved[i] = resolved[ref_idx]
                    changed = True
    return resolved


def parse_top_line():
    """Return list of {bu, week_start, planned_troas} for forecast rows."""
    print("Reading Top Line tab (formula render)...")
    rows = read_worksheet("Top Line", render="formula")
    print(f"  Got {len(rows)} rows")

    date_map = _resolve_date_chain(rows)
    print(f"  Resolved {len(date_map)} week dates from formula chain")

    results = []
    for i, r in enumerate(rows[3:], start=3):
        bu = r[1] if len(r) > 1 else ""
        x_val = r[23] if len(r) > 23 else ""

        if bu not in BUS_OF_INTEREST:
            continue
        if not x_val or str(x_val).startswith("="):
            continue

        try:
            planned_troas = float(x_val)
        except ValueError:
            continue

        wk = date_map.get(i)
        if not wk:
            continue

        results.append({
            "bu": bu,
            "week_start": wk,
            "planned_troas": planned_troas,
        })

    results.sort(key=lambda r: (r["bu"], r["week_start"]))
    return results


# ── 2. Load actual tROAS from Daily ROAS.xlsx ───────────────────
# Layout: 3 side-by-side BU sections
#   CORE: cols 1-4 (Date, Ad Spend, ROAS, LY ROAS)
#   Prof Certs: cols 6-9
#   INTL: cols 11-14
BU_COL_MAP = {
    "CORE":       {"date": 0, "roas": 2},   # 0-indexed
    "Prof Certs": {"date": 5, "roas": 7},
    "INTL":       {"date": 10, "roas": 12},
}


def load_roas_actuals(path=ROAS_FILE):
    """Return {bu_label: {week_start_date: avg_troas}} per BU."""
    print(f"Loading actual tROAS from {path}...")
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active

    all_rows = list(ws.iter_rows(min_row=3, values_only=True))  # skip header rows 1-2
    print(f"  {len(all_rows)} data rows")

    result = {}
    for bu_label, cols in BU_COL_MAP.items():
        date_idx = cols["date"]
        roas_idx = cols["roas"]

        daily = {}
        for row in all_rows:
            d = row[date_idx] if len(row) > date_idx else None
            t = row[roas_idx] if len(row) > roas_idx else None
            if d is None or t is None:
                continue
            if isinstance(d, datetime):
                d = d.date()
            try:
                t = float(t)
            except (ValueError, TypeError):
                continue
            if t > 0:
                daily[d] = t

        weekly = defaultdict(list)
        for d, t in sorted(daily.items()):
            dow = d.weekday()
            days_since_sun = (dow + 1) % 7
            ws_date = d - timedelta(days=days_since_sun)
            weekly[ws_date].append(t)

        bu_weekly = {}
        for ws_date, vals in weekly.items():
            bu_weekly[ws_date] = sum(vals) / len(vals)

        if daily:
            print(f"  {bu_label}: {len(daily)} daily => {len(bu_weekly)} weekly "
                  f"({min(daily.keys())} to {max(daily.keys())})")
        else:
            print(f"  {bu_label}: no data found")

        result[bu_label] = bu_weekly

    return result


# ── 2b. Compute dynamic paid mix from Supabase actuals ──────────
def _parse_money(v):
    if v is None:
        return 0.0
    try:
        return float(str(v).replace("$", "").replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def _fetch_all(table, params):
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    hdr = {"apikey": key, "Authorization": "Bearer " + key}
    rows = []
    limit = 1000
    offset = 0
    while True:
        p = {**params, "limit": str(limit), "offset": str(offset)}
        resp = httpx.get(f"{url}/rest/v1/{table}", headers=hdr, params=p, timeout=30)
        resp.raise_for_status()
        batch = resp.json()
        rows.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return rows


def compute_paid_mix(trailing_weeks=TRAILING_WEEKS_MIX):
    """Return {bu: paid_pct} from leads_weekly_actuals (trailing N weeks).

    Classifies Lead Source Groups as paid if they carry any ad spend
    across the full dataset, then computes the paid share of leads
    for the trailing window.
    """
    print(f"Computing paid mix from Supabase actuals (trailing {trailing_weeks} weeks)...")

    cutoff = (date.today() - timedelta(weeks=trailing_weeks)).isoformat()
    rows = _fetch_all("leads_weekly_actuals", {
        "select": '*',
        "order": '"Reporting Week".asc',
    })
    print(f"  Fetched {len(rows):,} total rows")

    # Step 1: classify lead source groups by spend (full history)
    lsg_spend = defaultdict(float)
    for r in rows:
        lsg = r.get("Lead Source Group", "")
        spend = _parse_money(r.get("Ad Spend  (Total, incl VSX)"))
        lsg_spend[lsg] += spend

    paid_groups = {lsg for lsg, s in lsg_spend.items() if s > 100}
    print(f"  Paid groups: {sorted(paid_groups)}")

    # Step 2: compute paid share for trailing window, per BU
    result = {}
    for bu in BUS_OF_INTEREST:
        paid = 0.0
        total = 0.0
        for r in rows:
            if r.get("Business") != bu:
                continue
            wk = r.get("Reporting Week", "")
            if wk < cutoff:
                continue
            leads = 0.0
            try:
                leads = float(r.get("Leads (Valid)") or 0)
            except (ValueError, TypeError):
                pass
            total += leads
            if r.get("Lead Source Group", "") in paid_groups:
                paid += leads

        pct = paid / total if total > 0 else 0.5
        result[bu] = round(pct, 4)
        print(f"  {bu}: {paid:,.0f} paid / {total:,.0f} total = {pct:.1%}")

    return result


# ── 3a. Fetch eLTV from Supabase + seasonal index ───────────────
def fetch_eltv_by_bu():
    """Return {bu: {date: eltv_per_client}} from marketing_eltv_actuals."""
    rows = _fetch_all("marketing_eltv_actuals", {
        "select": "week_start,business,eltv_per_client",
        "order": "week_start.asc",
    })
    result = defaultdict(dict)
    for r in rows:
        bu = r["business"]
        ws = r["week_start"]
        eltv = r.get("eltv_per_client")
        if eltv is not None:
            result[bu][date.fromisoformat(ws)] = float(eltv)
    print(f"  Loaded eLTV for {len(result)} BUs, "
          f"{sum(len(v) for v in result.values())} total week-BU rows")
    return dict(result)


def load_eltv_seasonal_index():
    """Load eltv_seasonal_index.csv -> {bu: {iso_week: seasonal_index}}"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eltv_seasonal_index.csv")
    result = defaultdict(dict)
    if not os.path.exists(path):
        print(f"  WARNING: {path} not found, eLTV seasonal forecasting disabled")
        return dict(result)
    with open(path) as f:
        for row in csv.DictReader(f):
            bu = row["business"]
            wk = int(row["iso_week"])
            result[bu][wk] = float(row["seasonal_index"])
    return dict(result)


def load_eltv_trailing_avg():
    """Load eltv_trailing_avg.csv -> {bu: trailing_avg}"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eltv_trailing_avg.csv")
    result = {}
    if not os.path.exists(path):
        return result
    with open(path) as f:
        for row in csv.DictReader(f):
            result[row["business"]] = float(row["trailing_avg"])
    return result


def _lookup_eltv(eltv_map, target_date, seasonal_idx=None, trailing_avg=None):
    """Find eLTV for a date. Falls back to seasonal forecast if no actual."""
    for offset in [0, -7, 7]:
        candidate = target_date + timedelta(days=offset)
        if candidate in eltv_map:
            return eltv_map[candidate], "actual"

    # Forecast using trailing avg * seasonal index
    if trailing_avg and seasonal_idx:
        iso_wk = target_date.isocalendar()[1]
        idx = seasonal_idx.get(iso_wk)
        if idx:
            forecast = trailing_avg * idx
            return round(forecast, 2), f"forecast(wk{iso_wk})"

    return None, ""


# ── 3b. Compute PY-comparison tROAS+eLTV adjustment ─────────────
def compute_adjustments(planned, actuals_by_bu, eltv_by_bu=None,
                        eltv_seasonal=None, eltv_trailing=None, trailing_weeks=13):
    """For each planned CY week, compare to PY same-week tROAS.

    The Supabase baseline uses PY leads in its YoY blend. Those PY leads
    were generated at PY tROAS levels. If CY planned tROAS differs,
    the YoY comparison is distorted by bid strategy -- not demand.

    Adjustment = PY_tROAS / CY_planned_tROAS
      > 1.0 = PY was less aggressive, CY plan is more aggressive => MORE leads
      < 1.0 = PY was more aggressive, CY plan is less aggressive => FEWER leads
    """
    results = []

    for bu in BUS_OF_INTEREST:
        roas_label = ROAS_BU_MAP.get(bu)
        actuals = actuals_by_bu.get(roas_label, {})
        bu_planned = [p for p in planned if p["bu"] == bu]
        if not bu_planned:
            continue

        actual_dates = sorted(actuals.keys())
        if not actual_dates:
            print(f"\n  {bu} ({roas_label}): no actual tROAS data found")
            for p in bu_planned:
                results.append({
                    "bu": bu, "week_start": p["week_start"],
                    "planned_troas": p["planned_troas"],
                    "py_troas": None, "adj_factor": 1.0, "paid_lead_pct": 0.0,
                    "source": "none",
                })
            continue

        print(f"\n  {bu} ({roas_label}):")
        print(f"    Actual data: {min(actual_dates)} to {max(actual_dates)}")

        # First pass: direct PY lookup (exact, then +/- 1 week)
        bu_results = []
        for p in bu_planned:
            wk = p["week_start"]
            pt = p["planned_troas"]
            wk_date = date.fromisoformat(str(wk))

            py_date = wk_date - timedelta(weeks=52)
            py_troas = None
            source = ""
            for offset in [0, -7, 7]:
                candidate = py_date + timedelta(days=offset)
                if candidate in actuals:
                    py_troas = actuals[candidate]
                    source = f"PY {candidate}"
                    break

            bu_results.append({
                "bu": bu,
                "week_start": wk,
                "planned_troas": pt,
                "py_troas": py_troas,
                "source": source,
            })

        # Second pass: interpolate gaps from nearest neighbors
        # If a week has no PY value but neighbors do, average them
        changed = True
        while changed:
            changed = False
            for idx in range(len(bu_results)):
                if bu_results[idx]["py_troas"] is not None:
                    continue
                # Find nearest prior value
                prev_val = None
                for j in range(idx - 1, -1, -1):
                    if bu_results[j]["py_troas"] is not None:
                        prev_val = bu_results[j]["py_troas"]
                        break
                # Find nearest next value
                next_val = None
                for j in range(idx + 1, len(bu_results)):
                    if bu_results[j]["py_troas"] is not None:
                        next_val = bu_results[j]["py_troas"]
                        break

                if prev_val is not None and next_val is not None:
                    bu_results[idx]["py_troas"] = (prev_val + next_val) / 2
                    bu_results[idx]["source"] = "interpolated"
                    changed = True
                elif next_val is not None:
                    bu_results[idx]["py_troas"] = next_val
                    bu_results[idx]["source"] = "nearest next"
                    changed = True
                elif prev_val is not None:
                    bu_results[idx]["py_troas"] = prev_val
                    bu_results[idx]["source"] = "nearest prev"
                    changed = True

        # Finalize adj_factor
        interp_count = sum(1 for r in bu_results if "interpolat" in r.get("source", "") or "nearest" in r.get("source", ""))
        if interp_count:
            print(f"    Interpolated {interp_count} missing PY weeks from neighbors")

        # eLTV lookup for this BU
        cy_eltv_map = (eltv_by_bu or {}).get(bu, {})
        py_eltv_map = cy_eltv_map  # same table, we look up PY date
        bu_seasonal = (eltv_seasonal or {}).get(bu, {})
        bu_trailing_avg = (eltv_trailing or {}).get(bu)

        # First: compute absolute ratios for every week
        enriched = []
        for r in bu_results:
            pt = r["planned_troas"]
            py = r["py_troas"]
            troas_ratio = py / pt if (py and pt) else 1.0

            wk_date = date.fromisoformat(str(r["week_start"]))
            cy_eltv, _ = _lookup_eltv(cy_eltv_map, wk_date,
                                       bu_seasonal, bu_trailing_avg)
            py_date = wk_date - timedelta(weeks=52)
            py_eltv, _ = _lookup_eltv(py_eltv_map, py_date)

            eltv_ratio = (cy_eltv / py_eltv) if (cy_eltv and py_eltv) else 1.0

            enriched.append({
                **r,
                "troas_ratio": troas_ratio,
                "cy_eltv": cy_eltv,
                "py_eltv": py_eltv,
                "eltv_ratio": eltv_ratio,
            })

        # Compute trailing 4-week average ratios
        # These represent "what the baseline already knows"
        TRAILING_N = 4
        today = date.today()
        current_week_start = today - timedelta(days=(today.weekday() + 1) % 7)
        if current_week_start > today:
            current_week_start -= timedelta(days=7)

        # Collect ratios for recent complete weeks from actuals
        trailing_troas_vals = []
        trailing_eltv_vals = []
        for wk_offset in range(1, TRAILING_N + 8):  # look back up to 8+4 weeks to find 4 valid
            wk_date = current_week_start - timedelta(weeks=wk_offset)
            wk_str = wk_date.isoformat()
            py_date = wk_date - timedelta(weeks=52)

            # tROAS: need PY actual and CY plan for this trailing week
            py_tr = None
            for off in [0, -7, 7]:
                cand = py_date + timedelta(days=off)
                if cand in actuals:
                    py_tr = actuals[cand]
                    break
            # Find the CY plan for this week from planned list
            cy_plan_tr = None
            for p in bu_planned:
                if str(p["week_start"]) == wk_str:
                    cy_plan_tr = p["planned_troas"]
                    break
            if cy_plan_tr is None and bu_planned:
                cy_plan_tr = bu_planned[0]["planned_troas"]

            if py_tr and cy_plan_tr:
                trailing_troas_vals.append(py_tr / cy_plan_tr)
            if len(trailing_troas_vals) >= TRAILING_N:
                break

        # eLTV trailing: use actual CY and PY from the same windows
        for wk_offset in range(1, TRAILING_N + 8):
            wk_date = current_week_start - timedelta(weeks=wk_offset)
            py_date = wk_date - timedelta(weeks=52)
            cy_e, _ = _lookup_eltv(cy_eltv_map, wk_date)
            py_e, _ = _lookup_eltv(py_eltv_map, py_date)
            if cy_e and py_e:
                trailing_eltv_vals.append(cy_e / py_e)
            if len(trailing_eltv_vals) >= TRAILING_N:
                break

        trail_troas = sum(trailing_troas_vals) / len(trailing_troas_vals) if trailing_troas_vals else 1.0
        trail_eltv = sum(trailing_eltv_vals) / len(trailing_eltv_vals) if trailing_eltv_vals else 1.0

        print(f"    Trailing {len(trailing_troas_vals)}-wk tROAS ratio (PY/CY): {trail_troas:.4f}")
        print(f"    Trailing {len(trailing_eltv_vals)}-wk eLTV ratio (CY/PY):  {trail_eltv:.4f}")

        # The Supabase baseline is a 50/50 blend of YoY and WoW components.
        # Our trailing anchor only covers the YoY half, so we weight the
        # delta at 50% to avoid over-adjusting the WoW portion.
        YOY_WEIGHT = 0.5

        for r in enriched:
            troas_delta = (r["troas_ratio"] / trail_troas) if trail_troas else 1.0
            eltv_delta = (r["eltv_ratio"] / trail_eltv) if trail_eltv else 1.0
            bid_delta_raw = troas_delta * eltv_delta
            bid_delta = 1.0 + YOY_WEIGHT * (bid_delta_raw - 1.0)
            combined_abs = r["eltv_ratio"] * r["troas_ratio"]

            results.append({
                "bu": r["bu"],
                "week_start": r["week_start"],
                "planned_troas": r["planned_troas"],
                "py_troas": round(r["py_troas"], 3) if r["py_troas"] else None,
                "adj_factor": round(r["troas_ratio"], 4),
                "cy_eltv": round(r["cy_eltv"], 2) if r["cy_eltv"] else None,
                "py_eltv": round(r["py_eltv"], 2) if r["py_eltv"] else None,
                "eltv_ratio": round(r["eltv_ratio"], 4),
                "combined_adj": round(combined_abs, 4),
                "trail_troas_ratio": round(trail_troas, 4),
                "trail_eltv_ratio": round(trail_eltv, 4),
                "troas_delta": round(troas_delta, 4),
                "eltv_delta": round(eltv_delta, 4),
                "bid_delta_raw": round(bid_delta_raw, 4),
                "bid_delta": round(bid_delta, 4),
                "paid_lead_pct": round((bid_delta - 1) * 100, 1),
                "source": r["source"],
            })

    return results


# ── 4. Output ────────────────────────────────────────────────────
def print_analysis(adjustments, paid_mix):
    print("\n")
    print("=" * 100)
    print("tROAS TARGET ADJUSTMENT -- CY PLANNED vs PY ACTUAL (REVIEW ONLY)")
    print("=" * 100)
    print()
    print("DELTA METHOD: only adjusts for how each future week's bid-strategy")
    print("ratio DIVERGES from the trailing 4-week ratio the baseline already reflects.")
    print()
    print("  bid_delta = (future_troas_ratio / trailing_troas_ratio)")
    print("            x (future_eltv_ratio  / trailing_eltv_ratio)")
    print("  ~1.0 = no change needed (baseline already reflects current regime)")
    print("  >1.0 = future week is MORE aggressive than recent trailing => more leads")
    print("  <1.0 = future week is LESS aggressive than recent trailing => fewer leads")
    print()

    for bu in BUS_OF_INTEREST:
        bu_rows = [r for r in adjustments if r["bu"] == bu]
        if not bu_rows:
            continue

        trail_t = bu_rows[0].get("trail_troas_ratio", 1.0)
        trail_e = bu_rows[0].get("trail_eltv_ratio", 1.0)

        print(f"\n{'-'*130}")
        print(f"  {bu}  |  Trailing tROAS ratio: {trail_t:.4f}  |  Trailing eLTV ratio: {trail_e:.4f}")
        print(f"{'-'*130}")
        print(f"  {'Week Start':<14} {'PY tROAS':>9} {'tROAS Rt':>9} {'tROAS D':>8} "
              f"{'CY eLTV':>9} {'PY eLTV':>9} {'eLTV Rt':>8} {'eLTV D':>8} "
              f"{'Raw D':>8} {'Wtd D':>8} {'Lead %':>9}")
        print(f"  {'-'*125}")

        for r in bu_rows:
            py_str = f"{r['py_troas']:.3f}" if r["py_troas"] else "n/a"
            cy_e = f"${r['cy_eltv']:,.0f}" if r.get("cy_eltv") else "n/a"
            py_e = f"${r['py_eltv']:,.0f}" if r.get("py_eltv") else "n/a"

            print(f"  {r['week_start']!s:<14} "
                  f"{py_str:>9} {r['adj_factor']:>9.4f} {r.get('troas_delta',1):>8.4f} "
                  f"{cy_e:>9} {py_e:>9} {r['eltv_ratio']:>8.4f} {r.get('eltv_delta',1):>8.4f} "
                  f"{r.get('bid_delta_raw',1):>8.4f} {r['bid_delta']:>8.4f} {r['paid_lead_pct']:>+8.1f}%")

        avg_delta = sum(r["bid_delta"] for r in bu_rows) / len(bu_rows)
        avg_pct = (avg_delta - 1) * 100
        print(f"  {'-'*125}")
        print(f"  {'AVERAGE':<14} {'':>9} {'':>9} {'':>8} {'':>9} {'':>9} {'':>8} {'':>8} "
              f"{'':>8} {avg_delta:>8.4f} {avg_pct:>+8.1f}%")

    print()
    print("=" * 100)
    print(f"TOTAL LEAD IMPACT ESTIMATE (paid mix from trailing {TRAILING_WEEKS_MIX}-week actuals)")
    print("=" * 100)
    print()

    for bu in BUS_OF_INTEREST:
        bu_rows = [r for r in adjustments if r["bu"] == bu]
        if not bu_rows:
            continue
        avg_bid_delta = sum(r["bid_delta"] for r in bu_rows) / len(bu_rows)
        avg_troas_d = sum(r.get("troas_delta", 1) for r in bu_rows) / len(bu_rows)
        avg_eltv_d = sum(r.get("eltv_delta", 1) for r in bu_rows) / len(bu_rows)
        mix = paid_mix.get(bu, 0.5)
        total_impact = mix * (avg_bid_delta - 1) * 100

        print(f"  {bu}:")
        print(f"    Avg tROAS delta:              {(avg_troas_d-1)*100:+.1f}%")
        print(f"    Avg eLTV delta:               {(avg_eltv_d-1)*100:+.1f}%")
        print(f"    Avg bid delta:                {(avg_bid_delta-1)*100:+.1f}%")
        print(f"    Paid mix (trailing actuals):  {mix:.1%}")
        print(f"    Total lead impact:            {total_impact:+.1f}%")
        print()


def main():
    planned = parse_top_line()
    print(f"  Found {len(planned)} planned tROAS entries across {len(set(r['bu'] for r in planned))} BUs")

    for bu in BUS_OF_INTEREST:
        bu_rows = [r for r in planned if r["bu"] == bu]
        if bu_rows:
            vals = set(r["planned_troas"] for r in bu_rows)
            print(f"    {bu}: {len(bu_rows)} weeks, targets: {sorted(vals)}")
            print(f"      Date range: {bu_rows[0]['week_start']} to {bu_rows[-1]['week_start']}")

    actuals_by_bu = load_roas_actuals()
    paid_mix = compute_paid_mix()

    print("\nFetching eLTV data from Supabase...")
    eltv_by_bu = fetch_eltv_by_bu()

    print("\nLoading eLTV seasonal index for forecasting...")
    eltv_seasonal = load_eltv_seasonal_index()
    eltv_trailing = load_eltv_trailing_avg()
    for bu, avg in eltv_trailing.items():
        print(f"  {bu}: trailing avg = ${avg:,.2f}")

    print("\nComputing adjustments (tROAS + eLTV)...")
    all_adj = compute_adjustments(planned, actuals_by_bu, eltv_by_bu=eltv_by_bu,
                                  eltv_seasonal=eltv_seasonal, eltv_trailing=eltv_trailing)

    print_analysis(all_adj, paid_mix)

    out_path = "troas_adjustments.csv"
    fieldnames = [
        "bu", "week_start", "planned_troas",
        "py_troas", "adj_factor",
        "cy_eltv", "py_eltv", "eltv_ratio", "combined_adj",
        "trail_troas_ratio", "trail_eltv_ratio",
        "troas_delta", "eltv_delta", "bid_delta_raw", "bid_delta",
        "paid_lead_pct", "source",
    ]
    out_rows = []
    for r in all_adj:
        out_rows.append({k: r.get(k) for k in fieldnames})
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out_rows)
    print(f"\nSaved to {out_path}")

    upsert_to_supabase(all_adj)


def upsert_to_supabase(adjustments):
    """Upsert adjustment rows to forecast_bid_adjustments in Supabase.

    Uses PostgREST's UPSERT (Prefer: resolution=merge-duplicates) on the
    (week_start, business) primary key so re-runs overwrite cleanly.
    """
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("\nSkipping Supabase upsert (no credentials)")
        return

    rows = []
    for r in adjustments:
        rows.append({
            "week_start": str(r["week_start"]),
            "business": r["bu"],
            "bid_delta": r.get("bid_delta", 1.0),
            "bid_delta_raw": r.get("bid_delta_raw"),
            "troas_ratio": r.get("adj_factor"),
            "eltv_ratio": r.get("eltv_ratio"),
            "troas_delta": r.get("troas_delta"),
            "eltv_delta": r.get("eltv_delta"),
            "planned_troas": r.get("planned_troas"),
            "py_troas": r.get("py_troas"),
            "cy_eltv": r.get("cy_eltv"),
            "py_eltv": r.get("py_eltv"),
            "trail_troas_ratio": r.get("trail_troas_ratio"),
            "trail_eltv_ratio": r.get("trail_eltv_ratio"),
            "source": r.get("source", ""),
        })

    print(f"\nUpserting {len(rows)} rows to forecast_bid_adjustments...")

    BATCH = 200
    upserted = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i : i + BATCH]
        resp = httpx.post(
            f"{url}/rest/v1/forecast_bid_adjustments",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
            json=batch,
            timeout=30,
        )
        if resp.status_code >= 400:
            print(f"  ERROR: HTTP {resp.status_code} — {resp.text}")
            return
        upserted += len(batch)

    print(f"  Upserted {upserted} rows to Supabase.")


if __name__ == "__main__":
    main()
