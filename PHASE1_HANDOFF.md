# Phase 1 handoff — what's done, what's blocked, what's on you

This document is the state of Phase 1. It exists so that when you come back to
this project next week (or next month), you know exactly where to pick up
without re-reading a chat transcript.

## Architecture changes since initial Phase 1 sprint

Two changes landed after the first pass that the rest of this doc reflects:

1. **Marketing namespacing.** Tables renamed `forecast_vintages` →
   `marketing_forecast_vintages` and `leads_daily_actuals` →
   `marketing_leads_daily_actuals` (plus their views) because this Supabase
   project is being shared with the rest of finance. See
   `supabase/migrations/2026-04-21_rename_marketing_tables.sql`.
2. **Plan-type-based auto-pick.** The memorialize source tab stays as the
   existing paste-values archive (`"Daily Forecast - LOCKED For Summary
   Tables"`), which carries multiple plans stacked (current SOP + OP + SOPM
   + historical SOPs — needed for `Weekly - Totals` model lookups). memorialize
   now takes `--plan-type` and **auto-picks the newest `plan_start_date`** for
   that type in the tab. Weekly command:
   `python memorialize_forecast.py --plan-type SOP --locked-by <you>`. Pass
   `--plan-start-date` too if you want to pin an older vintage.

## Phase 1 items — status

| # | Item | Status | Notes |
|---|---|---|---|
| 1 | Validate memorialization trigger approach | ✅ Done | GH Actions workflow `.github/workflows/memorialize_forecast.yml` is manual-trigger-only during the shadow period. Cron line for weekly automation is present but commented. |
| 2 | Settle `marketing_forecast_vintages` schema + validation layer | ✅ Done | Initial schema in `supabase/migrations/2026-04-20_forecast_vintages.sql`; rename + namespacing in `supabase/migrations/2026-04-21_rename_marketing_tables.sql`. Validation extracted to `vintage_validation.py` and wired into both `memorialize_forecast.py` and `reconcile_vintage.py`. |
| 3 | Python write function | ✅ Done | `memorialize_forecast.py` handles new locks, re-locks, dry runs, and auto-detects plan identity. `reconcile_vintage.py` diffs Supabase ↔ Sheet with the same auto-detect and exits non-zero on drift. |
| 4 | Run 2–4 weeks alongside publish + reconcile | ⏳ On you | Wall-clock only. 9 historical plans (175k rows) already backfilled; the shadow period clock starts when the first live weekly lock lands. |
| 5 | Stand up `marketing_leads_daily_actuals` table | 🟡 Schema done, ingest pending | Initial schema in `supabase/migrations/2026-04-21_leads_daily_actuals.sql`. Ingest path is a real design call — documented in `supabase/migrations/README.md` under "Ingest path decisions". |

## What you need to do (in order) to finish the rename

### 1. Apply the rename migration in Supabase Studio

[✅ done 2026-04-21 — verified in Supabase Table Editor that all five
`marketing_*` objects exist and no legacy names remain. Leaving the steps
below for re-running in dev.]

The first two migrations are already applied (the tables have data). The only
pending DB change is the rename:

```sql
-- Paste and run the full contents of:
--    supabase/migrations/2026-04-21_rename_marketing_tables.sql
```

It's wrapped in `BEGIN; ... COMMIT;`. If the SQL Editor prompts about RLS,
click "Run" (the rename is metadata-only; RLS state is preserved). Verify
with the queries in the migration's footer:

```sql
SELECT COUNT(*) FROM marketing_forecast_vintages;        -- expect ~175015
SELECT COUNT(*) FROM marketing_leads_daily_actuals;      -- expect 0
SELECT COUNT(*) FROM marketing_forecast_vs_actual_daily; -- expect ~25k
```

### 2. Dry-run the weekly ritual against the new names + archive tab

No Sheet changes needed. memorialize still reads `Daily Forecast - LOCKED
For Summary Tables`; it just now picks the newest `plan_start_date` for the
`--plan-type` you give it.

```
# Dry run (reads the tab, validates, writes nothing):
python memorialize_forecast.py --plan-type SOP --locked-by christian.thomas --dry-run

# Real lock after dry run passes:
python memorialize_forecast.py --plan-type SOP --locked-by christian.thomas \
  --notes "First post-rename SOP lock"

# Reconcile to confirm the round-trip:
python reconcile_vintage.py --plan-type SOP
```

If reconcile prints `MATCH`, the write path works end-to-end on the renamed
tables.

### 3. Commit the rename + Option B changes

```
git add \
  supabase/migrations/2026-04-20_forecast_vintages.sql \
  supabase/migrations/2026-04-21_leads_daily_actuals.sql \
  supabase/migrations/2026-04-21_rename_marketing_tables.sql \
  supabase/migrations/README.md \
  vintage_validation.py \
  memorialize_forecast.py \
  reconcile_vintage.py \
  backfill_vintages.py \
  read_from_sheets.py \
  .github/workflows/memorialize_forecast.yml \
  PHASE1_HANDOFF.md \
  PROJECT_MANIFEST.md \
  AGENTS.md
```

PR labels: `ai`, `cursor` (per workspace rules).

## Still-blocked / not-started items (carried over from prior session)

These predate Phase 1 and are blocked on Supabase org permissions. They don't
block Phase 1, but both should get unblocked together when you have a minute
with someone who has admin on the Supabase org:

- Fresh GitHub PAT stored as Supabase `GITHUB_PAT` secret.
- `supabase functions download bright-worker` + add the ~15-line GitHub
  `workflow_dispatch` call + redeploy.
- Move `push_forecast.yml` cron from 5:17 AM → 5:43 AM CT as the safety net
  once the event-driven path is live.
- Freshness check in `push_to_sheets.py` (abort if `leads_forecast` is stale).

Once those are done, `marketing_leads_daily_actuals` ingest (Phase 1 item 5)
can use the same Edge Function scaffolding — that's why the recommendation in
`supabase/migrations/README.md` is option 2 (new Looker schedule).

## What the GH Actions workflow does

`.github/workflows/memorialize_forecast.yml`:

- **Manual trigger only** right now. Go to Actions → "Memorialize Locked
  Forecast" → Run workflow. Required inputs: `plan_type` (SOP / OP / SOPM),
  `locked_by`. Optional: `plan_start_date` (blank = newest date for that
  plan_type in the tab), `notes`, `supersede_reason` (for re-locks),
  `strict`, `dry_run`.
- Runs `memorialize_forecast.py` with the inputs above.
- Runs `reconcile_vintage.py` as a post-step (unless it was a dry run).
  Reconcile failure will fail the job, which is intentional — you want the
  email/notification if the write didn't round-trip cleanly.
- Secrets it relies on (already exist in the repo): `SUPABASE_URL`,
  `SUPABASE_SERVICE_ROLE_KEY`, `GOOGLE_CREDENTIALS_JSON`.

When the shadow period is over, uncomment the `schedule:` block at the bottom
of the `on:` section:

```yaml
schedule:
  - cron: '0 12 * * 1'  # 7:00 AM CT Mondays during CDT
  # - cron: '0 13 * * 1'  # 7:00 AM CT Mondays during CST (Nov–Mar)
```

With plan-type-based auto-pick, cron runs only need a fixed `plan_type`
default (`SOP` for the weekly ritual) and a fixed `locked_by`. The script
picks the newest `plan_start_date` for `SOP` in the tab, so cron doesn't need
to compute "next Sunday" — the Sheet already has the right row set.

## Known limits / things I chose NOT to do

- **No pandas.** Both scripts are stdlib + `httpx` + `gspread`. The dataset is
  small (~a few thousand rows), pandas would be overkill, and keeping the
  dependency footprint tight makes CI fast.
- **No per-row `superseded_by` pairing.** Re-locks point every superseded row
  at a single canonical replacement id. Per-row pairing requires joining on
  the full business key, which is fragile if rows appear/disappear between
  locks. Audit can still trace history via `plan_type + plan_start_date +
  superseded_at`.
- **No ingest script for `marketing_leads_daily_actuals`.** The source of
  truth for daily paid/organic data is a design decision (see README).
  Writing a placeholder script would just be a TODO in disguise.
- **Sheet archive ↔ Supabase auto-sync not yet implemented.** The user
  still maintains `Daily Forecast - LOCKED For Summary Tables` by paste-
  values. A future downstream script could read
  `marketing_forecast_vintages_current` and rewrite the archive tab from
  Supabase (making Supabase strictly the source of truth). Not blocking
  today — user is happy with the manual paste in both directions.
