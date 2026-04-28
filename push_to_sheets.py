"""Push adjusted forecast from Supabase to Google Sheets.

Pipeline:
  1. Verify Looker ingest freshness
  2. Regenerate Supabase baseline via generate_forecast() RPC
  3. Fetch baseline from leads_forecast
  4. Apply adjustment layers:
     a. Holiday demand adjustment (w_deseas weightings)
     b. Bid-strategy adjustment (delta method: tROAS + eLTV)
  5. Push baseline + adjusted columns to Google Sheets
"""
import csv
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

load_dotenv()

# ── CONFIG ──────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
GOOGLE_CREDS_FILE = os.environ.get("GOOGLE_CREDS_FILE", "credentials.json")
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "Marketing Model - Live")
WORKSHEET_NAME = os.environ.get("WORKSHEET_NAME", "Supabase Forecast")
MAX_ACTUALS_AGE_DAYS = int(os.environ.get("MAX_ACTUALS_AGE_DAYS", "3"))

PAID_MIX_DEFAULT = 0.93
BUSINESSES = ["VT Core", "International", "Prof Certs"]

_HDR = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env file")
    exit(1)


# ── STEP 0: Verify Looker ingest is fresh ──────────────────────────
def check_actuals_freshness():
    """Abort if leads_weekly_actuals hasn't been refreshed recently."""
    print("Checking leads_weekly_actuals freshness...")

    resp = httpx.get(
        f"{SUPABASE_URL}/rest/v1/leads_weekly_actuals",
        headers=_HDR,
        params={
            "select": '"Reporting Date"',
            "order": '"Reporting Date".desc.nullslast',
            "limit": 1,
        },
        timeout=30,
    )

    if resp.status_code >= 400:
        print(f"  ERROR: freshness check HTTP {resp.status_code} — {resp.text}")
        print("  Cannot verify ingest state; aborting to avoid pushing stale data.")
        exit(1)

    rows = resp.json()
    if not rows:
        print("  ERROR: leads_weekly_actuals is EMPTY.")
        print("  Likely the Edge Function wiped the table but the insert step failed.")
        print("  Check Supabase Edge Function logs (bright-worker) for batch errors.")
        exit(1)

    max_date_str = rows[0].get("Reporting Date")
    if not max_date_str:
        print("  ERROR: Top row has no Reporting Date; table may be corrupt.")
        exit(1)

    max_date = datetime.strptime(max_date_str, "%Y-%m-%d").date()
    age_days = (date.today() - max_date).days

    if age_days > MAX_ACTUALS_AGE_DAYS:
        print(f"  ERROR: Most recent Reporting Date is {max_date} ({age_days} days old).")
        print(f"  Threshold is {MAX_ACTUALS_AGE_DAYS} days (set via MAX_ACTUALS_AGE_DAYS env var).")
        print("  This usually means the Looker webhook did not fire (or the Edge Function")
        print("  failed mid-ingest). Check Looker Schedules UI and Supabase Edge Function")
        print("  logs (bright-worker) before re-running.")
        exit(1)

    print(f"  Most recent Reporting Date: {max_date} ({age_days} days old). OK.")


# ── STEP 1: Run generate_forecast() in Supabase ────────────────────
def run_forecast():
    """Call generate_forecast() via Supabase RPC; abort on failure."""
    print("Running generate_forecast()...")
    try:
        resp = httpx.post(
            f"{SUPABASE_URL}/rest/v1/rpc/generate_forecast",
            headers={**_HDR, "Content-Type": "application/json"},
            json={},
            timeout=120,
        )
    except Exception as e:
        print(f"  ERROR: RPC call raised an exception: {e}")
        print("  Aborting to avoid pushing a potentially stale forecast.")
        print("  Run generate_forecast() manually in Supabase SQL Editor to investigate.")
        exit(1)

    if resp.status_code >= 400:
        print(f"  ERROR: RPC returned HTTP {resp.status_code}: {resp.text}")
        print("  Aborting to avoid pushing a potentially stale forecast.")
        print("  Run generate_forecast() manually in Supabase SQL Editor to investigate.")
        exit(1)

    print("  Forecast generated successfully.")


# ── STEP 2: Pull forecast data from Supabase ───────────────────────
def fetch_forecast():
    """Fetch all rows from leads_forecast, ordered by BU then week."""
    print("Fetching forecast data...")

    resp = httpx.get(
        f"{SUPABASE_URL}/rest/v1/leads_forecast",
        headers=_HDR,
        params={
            "select": "*",
            "order": "Business.asc,week_year.asc,week_number.asc",
            "limit": 10000,
        },
        timeout=60,
    )
    if resp.status_code >= 400:
        print(f"  ERROR fetching data: {resp.status_code} — {resp.text}")
        return []

    rows = resp.json()
    print(f"  Fetched {len(rows)} forecast rows.")
    return rows


# ── STEP 2b: Fetch adjustment inputs ───────────────────────────────
def fetch_calendar():
    """Fetch holiday calendar keyed by week_start.

    Returns a dict mapping week_start (ISO date string) to
    {"week_event_name": str|None, "week_has_full_week_impact": bool}.
    Keying by week_start avoids week_number/week_year convention
    mismatches between the calendar and forecast tables.
    """
    print("Fetching calendar events...")
    resp = httpx.get(
        f"{SUPABASE_URL}/rest/v1/marketing_calendar_weeks_current",
        headers=_HDR,
        params={
            "select": "week_start,week_event_name,week_has_full_week_impact",
            "order": "week_start.asc",
            "limit": "2000",
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        print(f"  WARNING: calendar fetch failed HTTP {resp.status_code}")
        return {}
    rows = resp.json()
    cal = {}
    hol_count = 0
    for r in rows:
        ws = r.get("week_start")
        if ws:
            cal[ws] = r
            if r.get("week_has_full_week_impact"):
                hol_count += 1
    print(f"  {len(cal)} calendar weeks, {hol_count} holiday weeks.")
    return cal


def load_holiday_weightings():
    """Read AVG rows from holiday_weightings.csv.

    Returns {(event_name, business): w_deseas}.
    The CSV is committed to the repo and available in CI.
    """
    print("Loading holiday weightings...")
    wt = {}
    csv_path = Path(__file__).parent / "holiday_weightings.csv"
    if not csv_path.exists():
        print(f"  WARNING: {csv_path} not found, holiday adjustments disabled.")
        return wt
    with open(csv_path) as f:
        for r in csv.DictReader(f):
            if r["year"] == "AVG" and r.get("w_deseas"):
                wt[(r["event_name"], r["business"])] = float(r["w_deseas"])
    print(f"  {len(wt)} event x BU weightings loaded.")
    return wt


def fetch_bid_adjustments():
    """Fetch bid_delta from forecast_bid_adjustments in Supabase.

    Returns {(week_start, business): bid_delta}.
    """
    print("Fetching bid-strategy adjustments...")
    resp = httpx.get(
        f"{SUPABASE_URL}/rest/v1/forecast_bid_adjustments",
        headers=_HDR,
        params={
            "select": "week_start,business,bid_delta",
            "order": "week_start.asc",
            "limit": "5000",
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        print(f"  WARNING: bid adjustments fetch failed HTTP {resp.status_code}")
        return {}
    rows = resp.json()
    result = {}
    for r in rows:
        bd = r.get("bid_delta")
        if bd is not None:
            result[(r["week_start"], r["business"])] = float(bd)
    print(f"  {len(result)} bid adjustment rows loaded.")
    return result


def fetch_paid_mix():
    """Compute paid lead % per BU from trailing actuals in Supabase.

    Falls back to PAID_MIX_DEFAULT if data is unavailable.
    """
    print("Computing paid mix from trailing actuals...")
    TRAILING_WEEKS = 13
    cutoff = (date.today() - timedelta(weeks=TRAILING_WEEKS)).isoformat()

    resp = httpx.get(
        f"{SUPABASE_URL}/rest/v1/leads_weekly_actuals",
        headers=_HDR,
        params={
            "select": '"Business","Lead Source Group","Leads (Valid)","Ad Spend  (Total, incl VSX)"',
            "limit": "50000",
        },
        timeout=120,
    )
    if resp.status_code >= 400:
        print(f"  WARNING: paid mix query failed, using default {PAID_MIX_DEFAULT}")
        return {bu: PAID_MIX_DEFAULT for bu in BUSINESSES}

    rows = resp.json()

    def _money(v):
        if v is None:
            return 0.0
        try:
            return float(str(v).replace("$", "").replace(",", ""))
        except (ValueError, TypeError):
            return 0.0

    lsg_spend = {}
    for r in rows:
        lsg = r.get("Lead Source Group", "")
        lsg_spend[lsg] = lsg_spend.get(lsg, 0) + _money(r.get("Ad Spend  (Total, incl VSX)"))
    paid_groups = {lsg for lsg, s in lsg_spend.items() if s > 100}

    result = {}
    for bu in BUSINESSES:
        paid = 0.0
        total = 0.0
        for r in rows:
            if r.get("Business") != bu:
                continue
            rw = r.get("Reporting Week", r.get("Reporting Date", ""))
            if rw and rw < cutoff:
                continue
            leads = float(r.get("Leads (Valid)") or 0)
            total += leads
            if r.get("Lead Source Group", "") in paid_groups:
                paid += leads
        pct = paid / total if total > 0 else PAID_MIX_DEFAULT
        result[bu] = round(pct, 4)
        print(f"  {bu}: {pct:.1%}")
    return result


# ── STEP 2c: Apply adjustments ──────────────────────────────────────
_WOW_YEARS = [2023, 2024, 2025]


def apply_adjustments(forecast_rows, cal, weightings, bid_adj, paid_mix):
    """Apply holiday + bid-strategy adjustments with cascade propagation.

    Three correction layers applied in order for each forecast week:

    1. **Holiday WoW correction** — replace contaminated avg_WoW_3yr
       with the average from years that actually had the same holiday.

    2. **Holiday YoY correction** — if PY doesn't match, correct via
       w_deseas; if PY was a different holiday, undo contamination.

    3. **Cascade propagation** — corrections in earlier weeks ripple
       forward through two channels of the baseline formula:
         a. trailing_4wk_yoy: adjusted CY leads change the YoY ratios
            used by weeks 1-4 ahead.
         b. prior_wk_fcst: adjusted baseline feeds the WoW component
            of the next week, and so on.

    Weeks are processed chronologically per BU so each week's total
    adjustment is available for downstream cascade computation.
    """
    from collections import defaultdict

    by_wn = {}
    for r in forecast_rows:
        wn = r.get("week_number")
        wy = r.get("week_year")
        if wn is not None and wy is not None:
            by_wn[(wn, wy, r["Business"])] = r

    bu_groups = defaultdict(list)
    for row in forecast_rows:
        bu_groups[row["Business"]].append(row)
    for bu in bu_groups:
        bu_groups[bu].sort(key=lambda r: r["week_start"])

    adj_map = {}

    adjusted_count = 0
    for bu, bu_rows in bu_groups.items():
        for row in bu_rows:
            ws = row["week_start"]
            wn = row.get("week_number")
            wy = row.get("week_year")
            bl = row.get("baseline_forecast") or 0
            is_actual = row.get("is_actual", False)
            py_leads = row.get("py_leads") or 0
            avg_wow = row.get("avg_wow_3yr") or 0

            hol_adj = 0.0
            cascade_adj = 0.0
            bid_adj_leads = 0.0

            if not is_actual and bl > 0 and ws and wn is not None:
                cy_cal = cal.get(ws, {})
                cy_hol = cy_cal.get("week_has_full_week_impact", False)
                cy_event = (cy_cal.get("week_event_name") or "")

                py_ws = (date.fromisoformat(ws) - timedelta(weeks=52)).isoformat()
                py_cal = cal.get(py_ws, {})
                py_hol = py_cal.get("week_has_full_week_impact", False)
                py_event = (py_cal.get("week_event_name") or "")

                prev_wn = wn - 1 if wn > 1 else 52
                prev_wy_adj = 0 if wn > 1 else -1

                # ── 1. Holiday WoW correction ──
                all_wows = []
                ref_hol_flags = []
                for yr in _WOW_YEARS:
                    this_r = by_wn.get((wn, yr, bu))
                    prev_r = by_wn.get((prev_wn, yr + prev_wy_adj, bu))
                    if this_r and prev_r:
                        t = this_r.get("baseline_forecast") or 0
                        p = prev_r.get("baseline_forecast") or 0
                        if p > 0:
                            ref_ws = this_r.get("week_start")
                            ref_c = cal.get(ref_ws, {})
                            ref_fw = ref_c.get("week_has_full_week_impact", False)
                            ref_ev = ref_c.get("week_event_name") or ""
                            all_wows.append(t / p)
                            ref_hol_flags.append((ref_fw, ref_ev))

                if all_wows:
                    prior_row = by_wn.get((prev_wn, wy + prev_wy_adj, bu))
                    prior_bl = (prior_row.get("baseline_forecast") or 0) if prior_row else 0

                    if cy_hol and cy_event:
                        keep = [w for w, (fw, ev) in zip(all_wows, ref_hol_flags)
                                if fw and ev == cy_event]
                        if keep and len(keep) < len(all_wows) and prior_bl > 0:
                            actual_avg = sum(all_wows) / len(all_wows)
                            corrected_avg = sum(keep) / len(keep)
                            hol_adj += 0.5 * prior_bl * (corrected_avg - actual_avg)

                    elif not cy_hol:
                        clean = [w for w, (fw, ev) in zip(all_wows, ref_hol_flags)
                                 if not fw]
                        if clean and len(clean) < len(all_wows) and prior_bl > 0:
                            actual_avg = sum(all_wows) / len(all_wows)
                            corrected_avg = sum(clean) / len(clean)
                            hol_adj += 0.5 * prior_bl * (corrected_avg - actual_avg)

                # ── 2. Holiday YoY correction ──
                if cy_hol and cy_event and not (py_hol and py_event == cy_event):
                    w_des = weightings.get((cy_event, bu))
                    if w_des and w_des != 1.0:
                        hol_adj += 0.5 * bl * (w_des - 1.0)

                if py_hol and py_event and py_event != cy_event:
                    w_des_py = weightings.get((py_event, bu))
                    if w_des_py and w_des_py != 1.0:
                        hol_adj += 0.5 * bl * (1.0 / w_des_py - 1.0)

                # ── 3. Cascade from prior weeks' adjustments ──
                # 3a. trailing_4wk_yoy: corrected CY leads change the
                #     YoY ratios that feed this week's YoY component.
                if py_leads > 0:
                    orig_ratios = []
                    corr_ratios = []
                    for i in range(1, 5):
                        tw = wn - i
                        ty = wy
                        if tw < 1:
                            tw += 52
                            ty -= 1
                        tr = by_wn.get((tw, ty, bu))
                        if tr:
                            t_bl = tr.get("baseline_forecast") or 0
                            t_py = tr.get("py_leads") or 0
                            t_ws = tr.get("week_start")
                            t_adj = adj_map.get((t_ws, bu), 0)
                            if t_py > 0 and t_bl > 0:
                                orig_ratios.append(t_bl / t_py)
                                corr_ratios.append((t_bl + t_adj) / t_py)

                    if orig_ratios:
                        orig_avg = sum(orig_ratios) / len(orig_ratios)
                        corr_avg = sum(corr_ratios) / len(corr_ratios)
                        delta = corr_avg - orig_avg
                        if abs(delta) > 0.0001:
                            cascade_adj += 0.5 * py_leads * delta

                # 3b. prior_wk_fcst: adjusted prior week changes the
                #     WoW component = 0.5 * prior_wk * avg_wow_3yr/100.
                #     (avg_wow_3yr is stored as a percentage in the DB)
                if avg_wow:
                    prev_r = by_wn.get((prev_wn, wy + prev_wy_adj, bu))
                    if prev_r:
                        prev_ws = prev_r.get("week_start")
                        prev_adj = adj_map.get((prev_ws, bu), 0)
                        if abs(prev_adj) > 0.01:
                            cascade_adj += 0.5 * prev_adj * (avg_wow / 100.0)

            combined = hol_adj + cascade_adj
            adj_map[(ws, bu)] = combined
            hol_bl = bl + combined

            if not is_actual:
                bd = bid_adj.get((ws, bu))
                if bd is not None and bd != 1.0:
                    mix = paid_mix.get(bu, PAID_MIX_DEFAULT)
                    bid_adj_leads = hol_bl * mix * (bd - 1)

            adjusted = bl + combined + bid_adj_leads
            row["holiday_adj"] = round(combined, 1)
            row["bid_strategy_adj"] = round(bid_adj_leads, 1)
            row["adjusted_forecast"] = round(adjusted, 1)

            if combined != 0 or bid_adj_leads != 0:
                adjusted_count += 1

    print(f"  Applied adjustments to {adjusted_count} forecast weeks.")
    return forecast_rows


# ── STEP 3: Push to Google Sheets ───────────────────────────────────
def push_to_sheets(rows):
    """Write forecast data to the Supabase Forecast worksheet."""
    print("Connecting to Google Sheets...")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
    gc = gspread.authorize(creds)

    sh = gc.open(SPREADSHEET_NAME)

    try:
        ws = sh.worksheet(WORKSHEET_NAME)
        print(f"  Found existing worksheet '{WORKSHEET_NAME}' — clearing it.")
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        print(f"  Creating new worksheet '{WORKSHEET_NAME}'.")
        ws = sh.add_worksheet(title=WORKSHEET_NAME, rows=len(rows) + 1, cols=20)

    columns = [
        "Business",
        "week_year",
        "week_number",
        "week_start",
        "is_actual",
        "actual_leads",
        "py_leads",
        "prior_week_forecast",
        "avg_wow_3yr",
        "trailing_4wk_yoy",
        "baseline_forecast",
        "holiday_adj",
        "bid_strategy_adj",
        "adjusted_forecast",
    ]

    header = columns
    data_rows = []
    for r in rows:
        data_rows.append([
            r.get("Business", ""),
            r.get("week_year", ""),
            r.get("week_number", ""),
            r.get("week_start", ""),
            r.get("is_actual", ""),
            r.get("actual_leads", ""),
            r.get("py_leads", ""),
            r.get("prior_week_forecast", ""),
            r.get("avg_wow_3yr", ""),
            r.get("trailing_4wk_yoy", ""),
            r.get("baseline_forecast", ""),
            r.get("holiday_adj", 0),
            r.get("bid_strategy_adj", 0),
            r.get("adjusted_forecast", r.get("baseline_forecast", "")),
        ])

    all_rows = [header] + data_rows

    print(f"  Writing {len(data_rows)} rows to '{WORKSHEET_NAME}'...")
    ws.update(range_name="A1", values=all_rows)
    print("  Done!")


# ── MAIN ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    check_actuals_freshness()
    run_forecast()

    forecast_rows = fetch_forecast()
    if not forecast_rows:
        print("No forecast data found. Run generate_forecast() in Supabase SQL Editor first.")
        exit(1)

    cal = fetch_calendar()
    weightings = load_holiday_weightings()
    bid_adj = fetch_bid_adjustments()
    paid_mix = fetch_paid_mix()

    forecast_rows = apply_adjustments(
        forecast_rows, cal, weightings, bid_adj, paid_mix,
    )

    push_to_sheets(forecast_rows)
    print("\nAll done! Check your Google Sheet.")
