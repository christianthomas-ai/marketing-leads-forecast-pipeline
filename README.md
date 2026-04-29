# Forecast Pipeline

Automated weekly leads forecasting pipeline for Varsity Tutors marketing. Replaces a manual Google Sheets model with a Supabase-backed system that computes a baseline forecast, applies holiday and bid-strategy adjustments, and pushes results to Google Sheets.

## Architecture

```
Looker webhook (daily 4:30 AM CT)
  -> Supabase Edge Function (bright-worker)
     -> wipes + reloads leads_weekly_actuals
     -> calls generate_forecast() RPC
  -> GitHub Actions cron (daily 5:17 AM CT)
     -> push_to_sheets.py
        -> re-runs generate_forecast()
        -> applies holiday + bid-strategy adjustments
        -> writes to Google Sheets "Supabase Forecast" tab
```

## Prerequisites

- Python 3.12+ (developed on 3.14)
- Supabase project access (Business Planning)
- Google Cloud service account with Sheets API access
- `Daily ROAS.xlsx` in Downloads (for tROAS analysis only)

## Local Setup

```bash
git clone https://github.com/christianthomas-ai/forecast-pipeline.git
cd forecast-pipeline
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux
pip install -r requirements.txt
cp .env.example .env         # fill in your credentials
```

## Key Scripts

| Script | Purpose | Run where |
|--------|---------|-----------|
| `push_to_sheets.py` | Fetch baseline, apply adjustments, push to Sheets | CI (daily) or local |
| `analyze_troas_adjustment.py` | Compute bid-strategy deltas, write CSV + upsert to Supabase | Local only |
| `_export_waterfall.py` | Generate Excel waterfall for human review | Local only |
| `memorialize_forecast.py` | Lock a forecast vintage to Supabase | CI (manual trigger) |
| `reconcile_vintage.py` | Reconcile a locked vintage | CI (manual trigger) |
| `load_calendar.py` | Sync marketing calendar from Sheets to Supabase | Local |
| `analyze_holiday_weightings.py` | Derive holiday demand weights from historical data | Local |
| `compute_eltv_seasonal.py` | Compute eLTV seasonal index | Local |

## Typical Workflow

### Daily (automated)
GitHub Actions runs `push_to_sheets.py` at 5:17 AM CT. No manual intervention needed.

### When tROAS targets or eLTV change
1. Update tROAS targets in the Google Sheets "Top Line" tab
2. Download fresh `Daily ROAS.xlsx` from Looker
3. Run: `python analyze_troas_adjustment.py`
   - Computes bid deltas and writes `troas_adjustments.csv`
   - Upserts adjustments to `forecast_bid_adjustments` table in Supabase
4. Next automated push will pick up the new adjustments

### When holiday weightings need updating
1. Run: `python analyze_holiday_weightings.py`
2. Review `holiday_weightings.csv`
3. Commit to repo (CI reads this file)

### Locking a forecast vintage (memorialization)
Memorialization saves a point-in-time snapshot of the locked forecast plan to
Supabase for audit and accuracy tracking. It is currently **manual trigger only**.

1. Go to **Actions > Memorialize Locked Forecast** in GitHub
2. Click **Run workflow**, fill in `plan_type` (e.g. SOP) and `locked_by`
3. The workflow reads the Google Sheet's locked tab, validates, and upserts to
   `marketing_forecast_vintages` in Supabase. A reconciliation step runs
   automatically afterward.
4. Check the workflow run for a green checkmark. If it fails, the error will
   be in the run log (usually a missing plan in the sheet or a validation issue).

**Enabling automatic weekly runs**: The schedule is ready but commented out in
`memorialize_forecast.yml`. To activate it, uncomment this line:

```yaml
  schedule:
    - cron: '0 12 * * 1'  # 12:00 UTC = 7:00 AM CDT / 6:00 AM CST Mondays
```

Note: GitHub Actions cron is UTC and routinely runs 1-6 hours late, so the
exact minute is aspirational. The DST shift (1 hour) is negligible compared
to GitHub's scheduling jitter.

## GitHub Actions

- **Push Forecast** (`push_forecast.yml`): Daily cron + manual dispatch. Pushes adjusted forecast to Sheets.
- **Memorialize Forecast** (`memorialize_forecast.yml`): Manual dispatch only (schedule ready to uncomment). Locks a forecast vintage + runs reconciliation.

## Deeper Documentation

1. [`PROJECT_MANIFEST.md`](PROJECT_MANIFEST.md) — Target architecture, roadmap, glossary
2. [`MODEL_STUDY_NOTES.md`](MODEL_STUDY_NOTES.md) — Detailed methodology: baseline formula, adjustment layers
3. [`AGENTS.md`](AGENTS.md) — Current-state architecture reference (tables, Edge Functions, timing chain)
4. [`PHASE1_HANDOFF.md`](PHASE1_HANDOFF.md) — Phase 1 status and reconciliation steps
5. [`supabase/migrations/README.md`](supabase/migrations/README.md) — Migration conventions and apply workflow
