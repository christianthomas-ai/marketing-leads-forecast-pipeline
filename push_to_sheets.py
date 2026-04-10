import httpx
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
import json
import os

# ── Load environment variables from .env file ──────────────────────
load_dotenv()

# ── CONFIG ──────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
GOOGLE_CREDS_FILE = os.environ.get("GOOGLE_CREDS_FILE", "credentials.json")
SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "Marketing Model - Live")
WORKSHEET_NAME = os.environ.get("WORKSHEET_NAME", "Supabase Forecast")

# Validate required env vars
if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in .env file")
    exit(1)

# ── STEP 1: Run generate_forecast() in Supabase ────────────────────
def run_forecast():
    """Call the generate_forecast() function via Supabase RPC."""
    print("Running generate_forecast()...")
    try:
        resp = httpx.post(
            f"{SUPABASE_URL}/rest/v1/rpc/generate_forecast",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
            },
            json={},
            timeout=120,
        )
        if resp.status_code >= 400:
            print(f"  WARNING: RPC returned {resp.status_code}. Run generate_forecast() manually in Supabase SQL Editor.")
            return False
        print("  Forecast generated successfully.")
        return True
    except Exception as e:
        print(f"  WARNING: Could not run forecast via RPC ({e}). Run manually in Supabase SQL Editor.")
        return False

# ── STEP 2: Pull forecast data from Supabase ───────────────────────
def fetch_forecast():
    """Fetch all rows from leads_forecast, ordered by BU then week."""
    print("Fetching forecast data...")

    resp = httpx.get(
        f"{SUPABASE_URL}/rest/v1/leads_forecast",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        },
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

    # Get or create the worksheet
    try:
        ws = sh.worksheet(WORKSHEET_NAME)
        print(f"  Found existing worksheet '{WORKSHEET_NAME}' — clearing it.")
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        print(f"  Creating new worksheet '{WORKSHEET_NAME}'.")
        ws = sh.add_worksheet(title=WORKSHEET_NAME, rows=len(rows) + 1, cols=15)

    # Define columns to push
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
        "forecast_adj",
        "final_forecast",
    ]

    # Build the grid: header row + data rows
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
            r.get("forecast_adj", ""),
            r.get("final_forecast", ""),
        ])

    all_rows = [header] + data_rows

    # Write in one batch
    print(f"  Writing {len(data_rows)} rows to '{WORKSHEET_NAME}'...")
    ws.update(range_name="A1", values=all_rows)
    print("  Done!")

# ── MAIN ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Step 1: Try to regenerate forecast via RPC
    run_forecast()

    # Step 2: Fetch forecast from Supabase
    forecast_rows = fetch_forecast()
    if not forecast_rows:
        print("No forecast data found. Run generate_forecast() in Supabase SQL Editor first.")
        exit(1)

    # Step 3: Push to Google Sheets
    push_to_sheets(forecast_rows)
    print("\nAll done! Check your Google Sheet.")