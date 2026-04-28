"""Derive per-BU x event holiday weightings from historical actuals,
with tROAS-adjusted paid-lead normalization.

Read-only analysis. WRITES NOTHING TO SUPABASE. Side-effect is a local
CSV file (``holiday_weightings.csv``).

Key concept: tROAS-adjusted weighting
--------------------------------------
Raw paid-lead counts during holidays reflect BOTH structural demand shifts
AND deliberate bid-target changes. When the paid team raises the tROAS
target (demands more return per dollar), volume drops not because demand
disappeared but because they chose to pull back.

To isolate structural demand we compute:

    adjusted_paid_leads = actual_paid_leads * (event_wk_tROAS / normal_wk_tROAS)

If the team raised the target from 2.7 to 3.6 during Thanksgiving, we
scale actual paid leads UP by 3.6/2.7 = 1.33x, reflecting "what volume
would have looked like at normal bid aggression." Then:

    w_paid_adj = adjusted_paid_leads_event / avg_normal_paid_leads

This is an imperfect but directionally correct first-order adjustment.
True elasticity (how much volume responds to a given tROAS change) is
nonlinear, but for the purpose of "is the weighting 0.55 or 0.75?" this
is a substantial improvement over ignoring bid changes entirely.

Data sources
------------
- leads_weekly_actuals (Supabase) — daily actuals by BU x Lead Source Group
- marketing_calendar_current (Supabase) — holiday tagging + week_start
- Daily ROAS.xlsx (local) — Summary By Day tab, blended tROAS per BU per day

Coverage limitations
--------------------
- tROAS data starts 2024-04-01, so Holy Week 2024 (3/24) has no data.
- Prof Certs + INTL tROAS starts mid-June 2025 only; adjustment is
  meaningful for CORE only right now.
- Pre-tROAS years (2022-2023) use unadjusted weightings.

Usage
-----
    python analyze_holiday_weightings.py

    python analyze_holiday_weightings.py --roas-file "path/to/Daily ROAS.xlsx"
    python analyze_holiday_weightings.py --csv-out custom_name.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
_PAGE = 1000
_HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}

_DEFAULT_ROAS_PATH = str(
    Path.home() / "Downloads" / "Daily ROAS.xlsx"
)

_DEFAULT_EVENTS = [
    "Thanksgiving Day",
    "Christmas Day",
    "New Year's Day",
    "Holy Week",
    "Spring Break 1",
    "Spring Break 2",
    "SAT",
    "ACT",
]

_BUSINESSES = ["VT Core", "International", "Prof Certs"]
_NA_ROLLUP = "VT Core NA"
_ANALYSIS_YEARS = [2022, 2023, 2024, 2025]
_BASELINE_WINDOWS = [4, 6]
_MIN_NORMAL_WEEKS = 3

# BU names in the ROAS file -> BU names in leads_weekly_actuals
_ROAS_BU_MAP = {
    "CORE": "VT Core",
    "INTL": "International",
    "Prof Certs": "Prof Certs",
}


def _die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _validate_env() -> None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        _die("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in env/.env")


def _parse_money(raw: Any) -> float:
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).replace("$", "").replace(",", "").strip()
    return float(s) if s else 0.0


# ── ROAS FILE LOAD ───────────────────────────────────────────────────

def load_roas_file(path: str) -> dict[tuple[date, str], dict[str, float]]:
    """Read Summary By Day tab. Returns {(date, bu_name): {spend, troas}}."""
    import openpyxl
    print(f"Loading tROAS from {path} ...")
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["Summary By Day"]

    # Layout: CORE=A-D, Prof Certs=F-I, INTL=K-N
    # Row 1 = BU headers, Row 2 = column headers, Row 3+ = data
    bu_blocks = [
        ("CORE",       0, 1, 2),   # date=col0, spend=col1, roas=col2
        ("Prof Certs", 5, 6, 7),
        ("INTL",      10, 11, 12),
    ]

    result: dict[tuple[date, str], dict[str, float]] = {}
    for row in ws.iter_rows(min_row=3, values_only=True):
        for roas_bu, date_col, spend_col, roas_col in bu_blocks:
            dt_val = row[date_col]
            roas_val = row[roas_col]
            if dt_val is None or not isinstance(dt_val, datetime):
                continue
            if roas_val is None:
                continue
            try:
                troas_f = float(roas_val)
            except (ValueError, TypeError):
                continue
            bu = _ROAS_BU_MAP[roas_bu]
            d = dt_val.date()
            spend = float(row[spend_col] or 0)
            result[(d, bu)] = {"spend": spend, "troas": troas_f}

    wb.close()
    by_bu = defaultdict(int)
    for (_, bu) in result:
        by_bu[bu] += 1
    for bu, n in sorted(by_bu.items()):
        print(f"  {bu}: {n} days with tROAS data")
    return result


# ── SUPABASE DATA LOAD ───────────────────────────────────────────────

def fetch_actuals(audience_filter: Optional[str] = None) -> list[dict[str, Any]]:
    label = f" (audience={audience_filter})" if audience_filter else ""
    print(f"Fetching leads_weekly_actuals{label} (paginated)...")
    all_rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        params: dict[str, Any] = {
            "select": "\"Reporting Date\",\"Business\","
                      "\"Leads (Valid)\",\"Ad Spend  (Total, incl VSX)\"",
            "limit": _PAGE,
            "offset": offset,
        }
        if audience_filter:
            params['"Audience (Sales)"'] = f"eq.{audience_filter}"
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/leads_weekly_actuals",
            headers=_HEADERS,
            params=params,
            timeout=120,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        all_rows.extend(batch)
        if len(batch) < _PAGE:
            break
        offset += _PAGE
        if offset % 20000 == 0:
            print(f"  ...{offset} rows")
    print(f"  fetched {len(all_rows)} rows.")
    return all_rows


def fetch_calendar() -> list[dict[str, Any]]:
    print("Fetching marketing_calendar_current...")
    all_rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        r = httpx.get(
            f"{SUPABASE_URL}/rest/v1/marketing_calendar_current",
            headers=_HEADERS,
            params={
                "select": "date,week_start,week_event_name,week_has_full_week_impact,"
                          "week_score_impact_name,week_has_score_impact,"
                          "score_release_type,score_impact_type",
                "limit": _PAGE,
                "offset": offset,
            },
            timeout=60,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        all_rows.extend(batch)
        if len(batch) < _PAGE:
            break
        offset += _PAGE
    print(f"  fetched {len(all_rows)} rows.")
    return all_rows


# ── TRANSFORM ────────────────────────────────────────────────────────

def roll_up_to_weekly(
    actuals: list[dict[str, Any]],
    calendar: list[dict[str, Any]],
    roas: dict[tuple[date, str], dict[str, float]],
) -> dict[tuple[date, str], dict[str, Any]]:
    """Aggregate daily actuals into weekly x BU buckets with tROAS overlay."""
    cal_by_date: dict[str, dict[str, Any]] = {r["date"]: r for r in calendar}

    def _empty() -> dict[str, Any]:
        return {
            "leads_total": 0.0,
            "spend_total": 0.0,
            "troas_spend_sum": 0.0,
            "troas_weighted_sum": 0.0,
            "troas_days": 0,
            "week_event_name": None,
            "week_has_full_week_impact": False,
            "week_score_impact_name": None,
            "week_has_score_impact": False,
        }

    buckets: dict[tuple[date, str], dict[str, Any]] = {}
    unmatched = 0

    for row in actuals:
        rep_date_str = row["Reporting Date"]
        cal_row = cal_by_date.get(rep_date_str)
        if cal_row is None:
            unmatched += 1
            continue
        week_start = datetime.strptime(cal_row["week_start"], "%Y-%m-%d").date()
        bu = row["Business"]
        leads = float(row.get("Leads (Valid)") or 0)
        spend = _parse_money(row.get("Ad Spend  (Total, incl VSX)"))

        key = (week_start, bu)
        b = buckets.setdefault(key, _empty())
        b["leads_total"]   += leads
        b["spend_total"]   += spend
        b["week_event_name"]           = cal_row["week_event_name"]
        b["week_has_full_week_impact"] = cal_row["week_has_full_week_impact"]
        b["week_score_impact_name"]    = cal_row.get("week_score_impact_name")
        b["week_has_score_impact"]     = cal_row.get("week_has_score_impact", False)

        # Overlay tROAS if available for this (date, BU)
        rep_date = datetime.strptime(rep_date_str, "%Y-%m-%d").date()
        roas_row = roas.get((rep_date, bu))
        if roas_row and roas_row["spend"] > 0:
            b["troas_spend_sum"]    += roas_row["spend"]
            b["troas_weighted_sum"] += roas_row["spend"] * roas_row["troas"]
            b["troas_days"]         += 1

    if unmatched:
        print(f"  NOTE: {unmatched} actuals rows with no calendar match, dropped.",
              file=sys.stderr)

    # Finalize weekly blended tROAS
    for b in buckets.values():
        if b["troas_spend_sum"] > 0:
            b["troas_blended"] = b["troas_weighted_sum"] / b["troas_spend_sum"]
        else:
            b["troas_blended"] = None

    return buckets


def add_na_rollup(
    buckets: dict[tuple[date, str], dict[str, Any]],
) -> dict[tuple[date, str], dict[str, Any]]:
    extras: dict[tuple[date, str], dict[str, Any]] = {}
    weeks = {ws for ws, _ in buckets.keys()}
    for ws in weeks:
        vtc = buckets.get((ws, "VT Core"))
        intl = buckets.get((ws, "International"))
        if vtc is None and intl is None:
            continue
        ref = vtc or intl
        # For NA rollup tROAS, use VT Core's (dominant spend)
        combined = {
            "leads_total":   (vtc["leads_total"]   if vtc else 0) + (intl["leads_total"]   if intl else 0),
            "spend_total":   (vtc["spend_total"]   if vtc else 0) + (intl["spend_total"]   if intl else 0),
            "troas_blended": vtc["troas_blended"] if vtc else None,
            "troas_days":    (vtc["troas_days"] if vtc else 0),
            "week_event_name":           ref["week_event_name"],
            "week_has_full_week_impact": ref["week_has_full_week_impact"],
            "week_score_impact_name":    ref.get("week_score_impact_name"),
            "week_has_score_impact":     ref.get("week_has_score_impact", False),
        }
        extras[(ws, _NA_ROLLUP)] = combined
    buckets.update(extras)
    return buckets


# ── SEASONAL INDEX ────────────────────────────────────────────────────

def build_seasonal_index(
    buckets: dict[tuple[date, str], dict[str, Any]],
    businesses: list[str],
) -> None:
    """Build a per-BU seasonal index and attach ``deseas`` to each bucket in-place.

    Seasonal index = mean(leads for ISO week) / grand_mean, computed only
    from non-holiday, non-score-impact weeks.  Each bucket gets a ``si``
    (seasonal index) and ``deseas`` (leads_total / si) field.
    """
    by_bu: dict[str, dict[date, dict[str, Any]]] = defaultdict(dict)
    for (ws, bu), val in buckets.items():
        by_bu[bu][ws] = val

    for bu in businesses:
        bu_weeks = by_bu.get(bu, {})
        if not bu_weeks:
            continue

        by_iso: dict[int, list[float]] = defaultdict(list)
        for ws, v in bu_weeks.items():
            if not v["week_has_full_week_impact"] and not v.get("week_has_score_impact"):
                by_iso[ws.isocalendar()[1]].append(v["leads_total"])

        week_avgs = {wk: mean(vals) for wk, vals in by_iso.items() if vals}
        gm = mean(week_avgs.values()) if week_avgs else 1.0
        si_map = {wk: avg / gm for wk, avg in week_avgs.items()}

        for ws, v in bu_weeks.items():
            s = si_map.get(ws.isocalendar()[1], 1.0)
            v["si"] = s
            v["deseas"] = v["leads_total"] / s if s else v["leads_total"]


# ── ANALYSIS ─────────────────────────────────────────────────────────

def _ratio(num: float, den: float) -> Optional[float]:
    return (num / den) if den else None


def compute_weightings(
    buckets: dict[tuple[date, str], dict[str, Any]],
    events: list[str],
    businesses: list[str],
) -> list[dict[str, Any]]:
    by_bu: dict[str, dict[date, dict[str, Any]]] = defaultdict(dict)
    for (ws, bu), val in buckets.items():
        by_bu[bu][ws] = val
    sorted_weeks: dict[str, list[date]] = {bu: sorted(w.keys()) for bu, w in by_bu.items()}

    results: list[dict[str, Any]] = []

    _SCORE_IMPACT_EVENTS = {"SAT", "ACT"}

    for event in events:
        is_score_event = event in _SCORE_IMPACT_EVENTS
        for bu in businesses:
            bu_weeks = sorted_weeks.get(bu, [])
            if not bu_weeks:
                continue

            if is_score_event:
                event_weeks = [
                    ws for ws in bu_weeks
                    if by_bu[bu][ws].get("week_score_impact_name") == event
                    and by_bu[bu][ws].get("week_has_score_impact")
                ]
            else:
                event_weeks = [
                    ws for ws in bu_weeks
                    if by_bu[bu][ws]["week_event_name"] == event
                    and by_bu[bu][ws]["week_has_full_week_impact"]
                ]
            if not event_weeks:
                continue

            per_year: list[dict[str, Any]] = []
            for ws in event_weeks:
                year = ws.year
                if year not in _ANALYSIS_YEARS:
                    continue
                ev = by_bu[bu][ws]

                idx = bu_weeks.index(ws)
                baseline_rows: list[dict[str, Any]] = []
                chosen_window = None
                for window in _BASELINE_WINDOWS:
                    lo = max(0, idx - window)
                    hi = min(len(bu_weeks), idx + window + 1)
                    candidate = []
                    for j in range(lo, hi):
                        if j == idx:
                            continue
                        neighbor = by_bu[bu][bu_weeks[j]]
                        if neighbor["week_has_full_week_impact"]:
                            continue
                        if neighbor.get("week_has_score_impact"):
                            continue
                        candidate.append(neighbor)
                    if len(candidate) >= _MIN_NORMAL_WEEKS:
                        baseline_rows = candidate
                        chosen_window = window
                        break

                row: dict[str, Any] = {
                    "event_name": event,
                    "business": bu,
                    "year": year,
                    "week_start": ws.isoformat(),
                    "event_leads": round(ev["leads_total"], 1),
                    "event_spend": round(ev["spend_total"], 0),
                    "event_troas": round(ev["troas_blended"], 3) if ev.get("troas_blended") else None,
                    "normal_wk_n": len(baseline_rows),
                    "window_wks": chosen_window,
                    "w_raw": None,
                    "w_deseas": None,
                    "w_troas_adj": None,
                    "w_deseas_troas": None,
                    "troas_ratio": None,
                    "spend_ratio": None,
                    "notes": "",
                }

                if len(baseline_rows) < _MIN_NORMAL_WEEKS:
                    row["notes"] = "insufficient normal weeks"
                    per_year.append(row)
                    continue

                avg_leads  = mean(b["leads_total"]  for b in baseline_rows)
                avg_spend  = mean(b["spend_total"]  for b in baseline_rows)

                avg_deseas = mean(b["deseas"] for b in baseline_rows)
                ev_deseas  = ev.get("deseas", ev["leads_total"])

                normal_troas_num = sum(
                    b["troas_blended"] * b["spend_total"]
                    for b in baseline_rows
                    if b.get("troas_blended") is not None and b["spend_total"] > 0
                )
                normal_troas_den = sum(
                    b["spend_total"]
                    for b in baseline_rows
                    if b.get("troas_blended") is not None and b["spend_total"] > 0
                )
                avg_troas = (normal_troas_num / normal_troas_den) if normal_troas_den else None

                row["normal_avg_leads"] = round(avg_leads, 1)
                row["normal_avg_spend"] = round(avg_spend, 0)
                row["normal_avg_troas"] = round(avg_troas, 3) if avg_troas else None

                row["w_raw"] = round(_ratio(ev["leads_total"], avg_leads) or 0, 4) if avg_leads else None

                row["w_deseas"] = round(_ratio(ev_deseas, avg_deseas) or 0, 4) if avg_deseas else None

                row["spend_ratio"] = round(_ratio(ev["spend_total"], avg_spend) or 0, 4) if avg_spend else None

                ev_troas = ev.get("troas_blended")
                if ev_troas and avg_troas:
                    troas_ratio = ev_troas / avg_troas
                    row["troas_ratio"] = round(troas_ratio, 4)

                    adjusted_leads = ev["leads_total"] * troas_ratio
                    row["w_troas_adj"] = round(_ratio(adjusted_leads, avg_leads) or 0, 4) if avg_leads else None

                    adjusted_deseas = ev_deseas * troas_ratio
                    row["w_deseas_troas"] = round(_ratio(adjusted_deseas, avg_deseas) or 0, 4) if avg_deseas else None
                else:
                    row["notes"] = (row["notes"] + " no tROAS data for this window").strip()

                per_year.append(row)

            results.extend(per_year)

            usable = [r for r in per_year if r["w_raw"] is not None]
            if usable:
                def _avg(k: str) -> Optional[float]:
                    vals = [r[k] for r in usable if r.get(k) is not None]
                    return round(mean(vals), 4) if vals else None
                def _sd(k: str) -> Optional[float]:
                    vals = [r[k] for r in usable if r.get(k) is not None]
                    return round(pstdev(vals), 4) if len(vals) > 1 else 0.0

                adj_usable = [r for r in usable if r.get("w_troas_adj") is not None]
                adj_note = f"n_adj={len(adj_usable)}" if adj_usable else "no tROAS overlap"

                results.append({
                    "event_name": event,
                    "business": bu,
                    "year": "AVG",
                    "week_start": "",
                    "event_leads":     round(mean(r["event_leads"] for r in usable), 1),
                    "event_spend":     round(mean(r["event_spend"] for r in usable), 0),
                    "event_troas":     _avg("event_troas"),
                    "normal_avg_leads": round(mean(r["normal_avg_leads"] for r in usable), 1),
                    "normal_avg_spend": round(mean(r["normal_avg_spend"] for r in usable), 0),
                    "normal_avg_troas": _avg("normal_avg_troas"),
                    "normal_wk_n":     round(mean(r["normal_wk_n"] for r in usable), 1),
                    "window_wks":      "",
                    "w_raw":           _avg("w_raw"),
                    "w_deseas":        _avg("w_deseas"),
                    "w_troas_adj":     _avg("w_troas_adj"),
                    "w_deseas_troas":  _avg("w_deseas_troas"),
                    "troas_ratio":     _avg("troas_ratio"),
                    "spend_ratio":     _avg("spend_ratio"),
                    "notes":           f"n={len(usable)} years, "
                                       f"stdev_raw={_sd('w_raw')}, "
                                       f"stdev_deseas={_sd('w_deseas')}, "
                                       f"{adj_note}",
                })

    return results


# ── OUTPUT ───────────────────────────────────────────────────────────

def print_summary(results: list[dict[str, Any]]) -> None:
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        by_event[r["event_name"]].append(r)

    for event in sorted(by_event.keys()):
        print(f"\n== {event} " + "=" * max(0, 80 - len(event)))
        by_bu: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in by_event[event]:
            by_bu[r["business"]].append(r)

        ordered = [_NA_ROLLUP] + _BUSINESSES
        for bu in ordered:
            if bu not in by_bu:
                continue
            rows = by_bu[bu]
            print(f"\n  {bu}")
            print(f"    {'year':<5} {'week_start':<12} "
                  f"{'w_raw':>7} {'w_des':>7} {'w_adj':>7} {'w_d+t':>7} "
                  f"{'tROAS_r':>8} "
                  f"{'spend_r':>8}  notes")
            year_rows = sorted([r for r in rows if r["year"] != "AVG"], key=lambda r: r["year"])
            avg_rows = [r for r in rows if r["year"] == "AVG"]
            for r in year_rows + avg_rows:
                def _f(v, fmt=".3f"):
                    return f"{v:{fmt}}" if v is not None else "    -"
                yr = str(r["year"])
                print(f"    {yr:<5} {r['week_start']:<12} "
                      f"{_f(r['w_raw']):>7} {_f(r['w_deseas']):>7} "
                      f"{_f(r['w_troas_adj']):>7} {_f(r.get('w_deseas_troas')):>7} "
                      f"{_f(r['troas_ratio']):>8} "
                      f"{_f(r['spend_ratio']):>8}  {r.get('notes','')}")


def write_csv(results: list[dict[str, Any]], path: str) -> None:
    fields = [
        "event_name", "business", "year", "week_start",
        "event_leads", "normal_avg_leads",
        "w_raw", "w_deseas",
        "event_spend", "normal_avg_spend", "spend_ratio",
        "event_troas", "normal_avg_troas", "troas_ratio",
        "w_troas_adj", "w_deseas_troas",
        "normal_wk_n", "window_wks", "notes",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in results:
            w.writerow(r)
    print(f"\nWrote {len(results)} rows to {path}")


# ── MAIN ─────────────────────────────────────────────────────────────

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--csv-out", default="holiday_weightings.csv")
    p.add_argument("--events", default=",".join(_DEFAULT_EVENTS))
    p.add_argument("--roas-file", default=_DEFAULT_ROAS_PATH,
                   help="Path to Daily ROAS.xlsx")
    p.add_argument("--audience", default=None,
                   help="Filter actuals to a single Audience (Sales) value, "
                        "e.g. 'K12 Test Prep'. Default: no filter (all audiences).")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    _validate_env()
    events = [e.strip() for e in args.events.split(",") if e.strip()]

    roas = load_roas_file(args.roas_file)
    actuals = fetch_actuals(audience_filter=args.audience)
    calendar = fetch_calendar()
    buckets = roll_up_to_weekly(actuals, calendar, roas)
    buckets = add_na_rollup(buckets)

    businesses = [_NA_ROLLUP] + _BUSINESSES
    build_seasonal_index(buckets, businesses)
    results = compute_weightings(buckets, events, businesses)

    if not results:
        print("\nNo event weeks matched.")
        return 0

    print_summary(results)
    write_csv(results, args.csv_out)

    print("\n-- Column glossary -------------------------------------")
    print("  w_raw      = event-week total leads / normal avg (unadjusted)")
    print("  w_des      = seasonally-normalized weighting")
    print("               deseasonalized_event / deseasonalized_baseline")
    print("               Removes ISO-week seasonal bias when holidays shift weeks.")
    print("  w_adj      = tROAS-adjusted weighting (raw leads, not deseasonalized)")
    print("               actual_leads * (event_tROAS / normal_tROAS) / normal_leads")
    print("  w_d+t      = RECOMMENDED: deseasonalized + tROAS-adjusted")
    print("               deseas_event * (event_tROAS / normal_tROAS) / deseas_baseline")
    print("               Isolates pure structural demand shift, free from both")
    print("               seasonal bias and bid-strategy changes.")
    print("  tROAS_r    = ev_tROAS / nm_tROAS (>1 = team pulled back bids)")
    print("  spend_r    = event spend / normal spend (<1 = less $ deployed)")
    print("  '-' in adj columns = no tROAS data for that window (pre-April 2024)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
