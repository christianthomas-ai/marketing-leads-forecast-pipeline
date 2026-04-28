"""Export forecast waterfall to Excel with one sheet per BU.

Now includes eLTV layer alongside tROAS:
  Combined Adj = (CY_eLTV / PY_eLTV) x (PY_tROAS / CY_tROAS)

Usage:
    python _export_waterfall.py
"""
import os, csv
from datetime import date, timedelta

import httpx
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, numbers
from openpyxl.utils import get_column_letter
from dotenv import load_dotenv

load_dotenv()

URL = os.environ["SUPABASE_URL"]
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
HDR = {"apikey": KEY, "Authorization": "Bearer " + KEY}

BUS = ["VT Core", "International", "Prof Certs"]
WEEK_MIN = "2026-04-19"
WEEK_MAX = "2026-12-27"
PAID_MIX_DEFAULT = 0.93

# ── Load Supabase forecast ───────────────────────────────────────
print("Loading Supabase baseline forecast...")
resp = httpx.get(f"{URL}/rest/v1/leads_forecast", headers=HDR, params={
    "select": "*", "order": "week_start.asc,Business.asc", "limit": "5000",
})
resp.raise_for_status()
_all_fc_rows = resp.json()
by_key = {(r["week_start"], r["Business"]): r for r in _all_fc_rows}
by_wn_bu = {}
for _r in _all_fc_rows:
    _wn = _r.get("week_number")
    _wy = _r.get("week_year")
    if _wn is not None and _wy is not None:
        by_wn_bu[(_wn, _wy, _r["Business"])] = _r


def get_bl(ws, bu):
    r = by_key.get((ws, bu))
    return r["baseline_forecast"] if r else 0


def get_py(ws, bu):
    """Get PY leads for a given week (52 weeks prior)."""
    wk_date = date.fromisoformat(ws)
    py_date = (wk_date - timedelta(weeks=52)).isoformat()
    r = by_key.get((py_date, bu))
    if r:
        return r.get("actual_leads") or r.get("baseline_forecast") or 0
    return 0


# ── Load calendar (keyed by week_start) ──────────────────────────
print("Loading calendar events...")
resp2 = httpx.get(f"{URL}/rest/v1/marketing_calendar_weeks_current", headers=HDR, params={
    "select": "week_start,week_event_name,week_has_full_week_impact",
    "order": "week_start.asc", "limit": "2000",
})
resp2.raise_for_status()
cal_by_ws = {r["week_start"]: r for r in resp2.json()}

# ── Load holiday weightings ─────────────────────────────────────
print("Loading holiday weightings...")
weightings = {}
with open("holiday_weightings.csv") as f:
    for r in csv.DictReader(f):
        if r["year"] == "AVG":
            key = (r["event_name"], r["business"])
            weightings[key] = {
                "w_deseas": float(r["w_deseas"]) if r.get("w_deseas") else None,
            }

# ── Load tROAS + eLTV adjustments (delta method) ──────────────
print("Loading tROAS + eLTV adjustments (delta method)...")
troas_adj = {}
with open("troas_adjustments.csv") as f:
    for r in csv.DictReader(f):
        py_troas = float(r["py_troas"]) if r.get("py_troas") else None
        cy_eltv = float(r["cy_eltv"]) if r.get("cy_eltv") else None
        py_eltv = float(r["py_eltv"]) if r.get("py_eltv") else None
        troas_adj[(r["week_start"], r["bu"])] = {
            "planned": float(r["planned_troas"]),
            "py_troas": py_troas,
            "troas_factor": float(r.get("adj_factor", 1.0)),
            "cy_eltv": cy_eltv,
            "py_eltv": py_eltv,
            "eltv_ratio": float(r.get("eltv_ratio", 1.0)),
            "combined_adj": float(r.get("combined_adj", 1.0)),
            "troas_delta": float(r.get("troas_delta", 1.0)),
            "eltv_delta": float(r.get("eltv_delta", 1.0)),
            "bid_delta": float(r.get("bid_delta", 1.0)),
            "source": r.get("source", ""),
        }

# ── Load paid mix ────────────────────────────────────────────────
print("Loading paid mix from Supabase actuals...")
paid_mix = {}
try:
    from analyze_troas_adjustment import compute_paid_mix
    paid_mix = compute_paid_mix()
except Exception as e:
    print(f"  Could not compute paid mix dynamically: {e}")
    for bu in BUS:
        paid_mix[bu] = PAID_MIX_DEFAULT


# ── Build week list ──────────────────────────────────────────────
all_weeks = sorted(set(ws for ws, bu in by_key.keys()))
target_weeks = [w for w in all_weeks if WEEK_MIN <= w <= WEEK_MAX]
print(f"  {len(target_weeks)} weeks in range")


# ── Build data for one BU ────────────────────────────────────────
def build_bu_rows(bu):
    rows = []
    mix = paid_mix.get(bu, PAID_MIX_DEFAULT)

    for ws in target_weeks:
        wk_date = date.fromisoformat(ws)
        iso_wk = wk_date.isocalendar()[1]

        bl = get_bl(ws, bu)
        py_ws = (wk_date - timedelta(weeks=52)).isoformat()

        cy_cal = cal_by_ws.get(ws, {})
        py_cal = cal_by_ws.get(py_ws, {})

        cy_hol = cy_cal.get("week_has_full_week_impact", False)
        py_hol = py_cal.get("week_has_full_week_impact", False)
        cy_event = cy_cal.get("week_event_name", "") or ""
        py_event = py_cal.get("week_event_name", "") or ""

        ename = cy_event or py_event
        w_des = None
        hol_adj = 0
        hol_bl = bl

        fc_row = by_key.get((ws, bu), {})
        wn = fc_row.get("week_number")
        wy = fc_row.get("week_year")

        if bl > 0 and wn is not None:
            prev_wn = wn - 1 if wn > 1 else 52
            prev_wy_adj = 0 if wn > 1 else -1

            all_wows = []
            ref_flags = []
            for yr in [2023, 2024, 2025]:
                this_r = by_wn_bu.get((wn, yr, bu))
                prev_r = by_wn_bu.get((prev_wn, yr + prev_wy_adj, bu))
                if this_r and prev_r:
                    t = this_r.get("baseline_forecast") or 0
                    p = prev_r.get("baseline_forecast") or 0
                    if p > 0:
                        ref_ws = this_r.get("week_start")
                        ref_c = cal_by_ws.get(ref_ws, {})
                        ref_fw = ref_c.get("week_has_full_week_impact", False)
                        ref_ev = ref_c.get("week_event_name") or ""
                        all_wows.append(t / p)
                        ref_flags.append((ref_fw, ref_ev))

            if all_wows:
                prior_r = by_wn_bu.get((prev_wn, wy + prev_wy_adj, bu))
                prior_bl = (prior_r.get("baseline_forecast") or 0) if prior_r else 0

                if cy_hol and cy_event:
                    keep = [w for w, (fw, ev) in zip(all_wows, ref_flags)
                            if fw and ev == cy_event]
                    if keep and len(keep) < len(all_wows) and prior_bl > 0:
                        actual_avg = sum(all_wows) / len(all_wows)
                        corrected_avg = sum(keep) / len(keep)
                        hol_adj += 0.5 * prior_bl * (corrected_avg - actual_avg)
                    w = weightings.get((cy_event, bu), {})
                    w_des = w.get("w_deseas")

                elif not cy_hol:
                    clean = [w for w, (fw, ev) in zip(all_wows, ref_flags)
                             if not fw]
                    if clean and len(clean) < len(all_wows) and prior_bl > 0:
                        actual_avg = sum(all_wows) / len(all_wows)
                        corrected_avg = sum(clean) / len(clean)
                        hol_adj += 0.5 * prior_bl * (corrected_avg - actual_avg)

            if cy_hol and cy_event and not (py_hol and py_event == cy_event):
                w = weightings.get((cy_event, bu), {})
                w_des = w.get("w_deseas")
                if w_des and w_des != 1.0:
                    hol_adj += 0.5 * bl * (w_des - 1.0)

            if py_hol and py_event and py_event != cy_event:
                w = weightings.get((py_event, bu), {})
                w_des_py = w.get("w_deseas")
                if w_des_py and w_des_py != 1.0:
                    hol_adj += 0.5 * bl * (1.0 / w_des_py - 1.0)

            hol_bl = bl + hol_adj

        hol_pct = hol_adj / bl * 100 if bl else 0

        ta = troas_adj.get((ws, bu))
        py_troas = ta["py_troas"] if ta else None
        cy_plan = ta["planned"] if ta else None
        troas_factor = ta["troas_factor"] if ta else 1.0
        cy_eltv = ta["cy_eltv"] if ta else None
        py_eltv = ta["py_eltv"] if ta else None
        eltv_ratio = ta["eltv_ratio"] if ta else 1.0
        bid_delta = ta["bid_delta"] if ta else 1.0

        bid_imp = hol_bl * mix * (bid_delta - 1) if ta else 0
        bid_pct = bid_imp / bl * 100 if bl else 0

        final = hol_bl + bid_imp
        vs_bl = final - bl
        total_pct = vs_bl / bl * 100 if bl else 0

        py_leads = get_py(ws, bu)
        bl_yoy = (bl / py_leads - 1) if py_leads else None
        final_yoy = (final / py_leads - 1) if py_leads else None

        rows.append({
            "week": ws,
            "iso_wk": iso_wk,
            "event": ename,
            "bl": bl,
            "scenario": ("both" if (cy_hol and py_hol and cy_event == py_event)
                         else "CY-only" if (cy_hol and not py_hol)
                         else "PY-only" if (py_hol and not cy_hol)
                         else "cross" if (cy_hol and py_hol)
                         else ""),
            "w_deseas": w_des,
            "hol_adj": hol_adj,
            "hol_pct": hol_pct,
            "py_troas": py_troas,
            "cy_plan": cy_plan,
            "troas_factor": troas_factor,
            "cy_eltv": cy_eltv,
            "py_eltv": py_eltv,
            "eltv_ratio": eltv_ratio,
            "bid_delta": bid_delta,
            "bid_adj": bid_imp,
            "bid_pct": bid_pct,
            "final": final,
            "vs_bl": vs_bl,
            "total_pct": total_pct,
            "py_leads": py_leads,
            "bl_yoy": bl_yoy,
            "final_yoy": final_yoy,
        })
    return rows


# ── Excel formatting ─────────────────────────────────────────────
HEADERS = [
    "Week Start", "ISO Wk", "Event",
    "Supabase\nBaseline", "Holiday\nScenario", "Holiday Wt\n(w_deseas)",
    "Holiday Adj\n(Leads)", "Holiday\nAdj %",
    "PY tROAS", "CY Plan\ntROAS", "tROAS\nRatio",
    "CY eLTV", "PY eLTV", "eLTV\nRatio",
    "Bid Delta\n(vs trailing)",
    "Bid Adj\n(Leads)", "Bid\nAdj %",
    "Final\nAdjusted", "vs Baseline\n(Leads)", "Total\nAdj %",
    "PY\nLeads", "Baseline\nYoY", "Final\nYoY",
]

HEADER_FONT = Font(bold=True, size=10)
HEADER_FILL = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
HOLIDAY_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
NEG_FONT = Font(color="C00000", size=10)
POS_FONT = Font(color="006100", size=10)
NUM_FMT_INT = '#,##0'
NUM_FMT_PCT = '+0.0%;-0.0%'
NUM_FMT_SIGN_INT = '+#,##0;-#,##0;0'
NUM_FMT_TROAS = '0.000'
NUM_FMT_FACTOR = '0.0000'
NUM_FMT_MONEY = '$#,##0'

COL_WIDTHS = [12, 7, 18, 13, 13, 12, 13, 11, 10, 10, 10, 10, 10, 10, 12, 12, 10, 13, 13, 10, 12, 11, 11]


def write_sheet(ws_sheet, bu, rows):
    for c_idx, h in enumerate(HEADERS, 1):
        cell = ws_sheet.cell(row=1, column=c_idx, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", wrap_text=True, vertical="bottom")

    for r_idx, row in enumerate(rows, 2):
        is_hol = bool(row["event"])

        vals = [
            row["week"], row["iso_wk"], row["event"] or "",
            row["bl"], row["scenario"], row["w_deseas"],
            row["hol_adj"], row["hol_pct"] / 100 if row["hol_pct"] else 0,
            row["py_troas"], row["cy_plan"], row["troas_factor"],
            row["cy_eltv"], row["py_eltv"], row["eltv_ratio"],
            row["bid_delta"],
            row["bid_adj"], row["bid_pct"] / 100 if row["bid_pct"] else 0,
            row["final"], row["vs_bl"], row["total_pct"] / 100 if row["total_pct"] else 0,
            row["py_leads"], row["bl_yoy"], row["final_yoy"],
        ]

        for c_idx, val in enumerate(vals, 1):
            cell = ws_sheet.cell(row=r_idx, column=c_idx, value=val)

            if is_hol:
                cell.fill = HOLIDAY_FILL

        # Number formats
        for c in [4]:  # BL
            ws_sheet.cell(row=r_idx, column=c).number_format = NUM_FMT_INT
        for c in [7]:  # Holiday Adj Leads
            ws_sheet.cell(row=r_idx, column=c).number_format = NUM_FMT_SIGN_INT
        for c in [8, 17, 20]:  # Pct columns
            ws_sheet.cell(row=r_idx, column=c).number_format = NUM_FMT_PCT
        for c in [9, 10]:  # tROAS values
            ws_sheet.cell(row=r_idx, column=c).number_format = NUM_FMT_TROAS
        for c in [11, 14, 15]:  # ratio/factor columns
            ws_sheet.cell(row=r_idx, column=c).number_format = NUM_FMT_FACTOR
        for c in [12, 13]:  # eLTV dollar values
            ws_sheet.cell(row=r_idx, column=c).number_format = NUM_FMT_MONEY
        for c in [16]:  # Bid adj leads
            ws_sheet.cell(row=r_idx, column=c).number_format = NUM_FMT_SIGN_INT
        for c in [18]:  # Final
            ws_sheet.cell(row=r_idx, column=c).number_format = NUM_FMT_INT
        for c in [19]:  # vs BL
            ws_sheet.cell(row=r_idx, column=c).number_format = NUM_FMT_SIGN_INT
        for c in [21]:  # PY Leads
            ws_sheet.cell(row=r_idx, column=c).number_format = NUM_FMT_INT
        for c in [22, 23]:  # Baseline YoY, Final YoY
            ws_sheet.cell(row=r_idx, column=c).number_format = NUM_FMT_PCT

    # Column widths
    for c_idx, w in enumerate(COL_WIDTHS, 1):
        ws_sheet.column_dimensions[get_column_letter(c_idx)].width = w

    # Freeze header row
    ws_sheet.freeze_panes = "A2"

    # Totals row
    tot_row = len(rows) + 2
    ws_sheet.cell(row=tot_row, column=1, value="TOTAL").font = Font(bold=True, size=10)
    for c in [4, 7, 16, 18, 19, 21]:
        col_letter = get_column_letter(c)
        cell = ws_sheet.cell(row=tot_row, column=c)
        cell.value = f"=SUM({col_letter}2:{col_letter}{tot_row-1})"
        cell.font = Font(bold=True, size=10)
        cell.number_format = NUM_FMT_SIGN_INT if c in [7, 16, 19] else NUM_FMT_INT
    # Total YoY = Total Final / Total PY
    for c, num, den in [(22, 4, 21), (23, 18, 21)]:
        num_letter = get_column_letter(num)
        den_letter = get_column_letter(den)
        cell = ws_sheet.cell(row=tot_row, column=c)
        cell.value = f"={num_letter}{tot_row}/{den_letter}{tot_row}-1"
        cell.font = Font(bold=True, size=10)
        cell.number_format = NUM_FMT_PCT


# ── Main ──────────────────────────────────────────────────────────
print("\nBuilding Excel workbook...")
wb = openpyxl.Workbook()
wb.remove(wb.active)

for bu in BUS:
    print(f"  {bu}...")
    rows = build_bu_rows(bu)
    ws_sheet = wb.create_sheet(title=bu)
    write_sheet(ws_sheet, bu, rows)

out_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "Forecast Waterfall by BU (Delta Method).xlsx",
)
wb.save(out_path)
print(f"\nSaved to: {out_path}")
print("Done.")
