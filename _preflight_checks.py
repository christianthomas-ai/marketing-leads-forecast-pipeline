"""Pre-production sanity checks for the waterfall methodology."""
import os, csv
from datetime import date, timedelta
import httpx
from dotenv import load_dotenv

load_dotenv()
URL = os.environ["SUPABASE_URL"]
KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
HDR = {"apikey": KEY, "Authorization": "Bearer " + KEY}

print("=" * 80)
print("CHECK 1: 2026 full-week-impact holidays in calendar")
print("=" * 80)
r = httpx.get(f"{URL}/rest/v1/marketing_calendar_weeks_current", headers=HDR, params={
    "select": "week_start,week_event_name,week_has_full_week_impact",
    "week_has_full_week_impact": "eq.true",
    "week_start": "gte.2026-01-01",
    "order": "week_start.asc", "limit": "50",
})
holidays_2026 = r.json()
for h in holidays_2026:
    print(f"  {h['week_start']}  {h['week_event_name']}")
print(f"  Total: {len(holidays_2026)} holiday weeks in 2026+")

# Which are in our Apr-Dec window?
in_window = [h for h in holidays_2026 if "2026-04-19" <= h["week_start"] <= "2026-12-27"]
print(f"  In export window (Apr 19 - Dec 27): {len(in_window)}")
for h in in_window:
    print(f"    {h['week_start']}  {h['week_event_name']}")

print()
print("=" * 80)
print("CHECK 2: Holiday weightings coverage by BU")
print("=" * 80)
weightings = {}
with open("holiday_weightings.csv") as f:
    for row in csv.DictReader(f):
        if row["year"] == "AVG":
            key = (row["event_name"], row["business"])
            weightings[key] = row.get("w_deseas", "")

for event in sorted(set(e for e, b in weightings.keys())):
    for bu in ["VT Core", "International", "Prof Certs"]:
        val = weightings.get((event, bu), "MISSING")
        status = "OK" if val and val != "MISSING" else "MISSING"
        print(f"  {event:25s} | {bu:15s} | w_deseas = {val if val else 'empty':>8} | {status}")

print()
print("=" * 80)
print("CHECK 3: tROAS interpolation spot-check (INTL + Prof Certs, early weeks)")
print("=" * 80)
adj = list(csv.DictReader(open("troas_adjustments.csv")))
for bu in ["International", "Prof Certs"]:
    print(f"\n  {bu}:")
    bu_rows = [r for r in adj if r["bu"] == bu and r["week_start"] <= "2026-07-01"]
    for r in bu_rows:
        src = r.get("source", "")
        marker = " <<<" if "interpolat" in src or "nearest" in src else ""
        print(f"    {r['week_start']}  PY={r['py_troas']:>6}  plan={r['planned_troas']}  "
              f"adj={r['adj_factor']}  src={src}{marker}")

print()
print("=" * 80)
print("CHECK 4: Prof Certs tROAS target change point")
print("=" * 80)
pc_rows = [r for r in adj if r["bu"] == "Prof Certs"]
prev_plan = None
for r in pc_rows:
    plan = r["planned_troas"]
    if plan != prev_plan:
        print(f"  Target changes to {plan} at week {r['week_start']}")
    prev_plan = plan

print()
print("=" * 80)
print("CHECK 5: Week alignment - first/last weeks across data sources")
print("=" * 80)
# Supabase forecast
r2 = httpx.get(f"{URL}/rest/v1/leads_forecast", headers=HDR, params={
    "select": "week_start", "order": "week_start.asc", "limit": "1",
})
r3 = httpx.get(f"{URL}/rest/v1/leads_forecast", headers=HDR, params={
    "select": "week_start", "order": "week_start.desc", "limit": "1",
})
print(f"  Supabase forecast:   {r2.json()[0]['week_start']} to {r3.json()[0]['week_start']}")

# tROAS adjustments
all_weeks = sorted(set(r["week_start"] for r in adj))
print(f"  tROAS adjustments:   {all_weeks[0]} to {all_weeks[-1]}")

# Holiday weightings years
years = set()
with open("holiday_weightings.csv") as f:
    for row in csv.DictReader(f):
        if row["year"] != "AVG":
            years.add(row["year"])
print(f"  Holiday weighting years: {sorted(years)}")

print()
print("=" * 80)
print("CHECK 6: Double-count risk assessment")
print("=" * 80)
print("""
  The Supabase baseline = (PY_leads * trailing_4wk_YoY + prior_wk_fcst * avg_WoW_3yr) / 2

  HOLIDAY LAYER (w_deseas):
    - Applied to a "normal week" median of surrounding non-holiday baselines
    - If PY comp week was ALSO a holiday (e.g. Thanksgiving vs Thanksgiving),
      the baseline already captures most of the holiday dip in its YoY.
    - The holiday adj will be SMALL for matched holidays, LARGE for mismatched.
    => LOW double-count risk for matched holidays (adj is a residual correction)
    => This is the INTENDED behavior.

  tROAS LAYER (PY actual / CY planned):
    - The baseline's YoY component already reflects PY tROAS implicitly
      (PY leads were generated at PY tROAS levels).
    - The trailing 4-week YoY captures the RECENT CY-vs-PY lead ratio,
      which includes BOTH demand AND tROAS effects.
    - Applying PY/CY tROAS adjustment ON TOP could double-count if the
      trailing YoY already reflects the tROAS regime change.

  VERDICT:
    The tROAS layer is best used as an EXPLANATORY decomposition
    ("X% of the YoY decline is tROAS-driven") rather than as a
    CUMULATIVE adjustment added on top of the baseline.

    If used cumulatively, it would overstate the adjustment for weeks
    where the trailing YoY already reflects the tROAS shift.

  RECOMMENDATION:
    For PRODUCTION forecasting: use the Supabase baseline + holiday adj only.
    The tROAS column should be INFORMATIONAL (explains what's driving YoY).
    Alternatively, if you want to use tROAS as an adjustment, the baseline
    should be rebuilt to strip out tROAS effects first.
""")

print("All checks complete.")
