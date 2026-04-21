# Supabase migrations

Canonical SQL changes to the Supabase schema for this project. Every structural
change to the database (new tables, new columns, new indexes, new views,
renames, drops) gets its own file here so the schema history is reviewable in
git.

## Naming convention

```
<YYYY-MM-DD>_<short_description>.sql
```

- Filenames sort chronologically, which is also the correct apply order.
- Use underscores, not spaces. Keep the description brief but specific
  (`forecast_vintages` beats `add_table`).
- Prefix domain-owned tables with the owning team once the project hosts more
  than one (e.g. `marketing_forecast_vintages` vs `finance_budget_lines`), so
  the Supabase UI reads as "whose table is this" at a glance.

Examples:

- `2026-04-20_forecast_vintages.sql`
- `2026-04-21_rename_marketing_tables.sql`
- `2026-05-04_add_troas_target_to_marketing_forecast_vintages.sql`
- `2026-06-01_holiday_calendar.sql`

## How to apply a migration

These migrations are not auto-applied. Today the workflow is manual:

1. Review the SQL file in this folder. Leave inline questions as
   `-- QUESTION: ...` if anything needs discussion before running.
2. Open Supabase Studio → SQL Editor (the "Business Planning" project).
3. Paste the full file contents, including the `BEGIN; ... COMMIT;` wrapper.
4. Run it. A clean run means every statement committed as a single
   transaction — partial failures roll back automatically.
5. Confirm the change in Supabase Studio → Table Editor.
6. Commit the migration file to git as part of the PR that depends on it.

## Conventions

- **Wrap each migration in `BEGIN; ... COMMIT;`** so a failure mid-file
  doesn't leave the database half-updated.
- **Do not include `DROP` statements** unless the migration is
  explicitly a removal. Accidental drops in prod are the #1 way a manual
  workflow loses data.
- **Comment heavily.** These files are also the schema documentation.
  If a column name is non-obvious, a column comment (`COMMENT ON COLUMN ...`)
  makes the intent discoverable from `psql` or Supabase Studio without
  digging back into git history.
- **One logical change per file.** Multiple unrelated changes in one
  migration are hard to review and hard to roll back.

## When (not if) we migrate to a migrations tool

At some point this will outgrow the manual workflow and want a real tool
(supabase CLI migrations, Flyway, sqitch, etc.). When that happens the file
format above is close enough to any of those tools that migration is cheap.
No need to adopt one today.

## Ingest path decisions

Some migrations add tables whose ingest pipeline is a separate, pending
decision. Those decisions live here so the migration file doesn't get
polluted with open questions.

### `2026-04-21_leads_daily_actuals.sql` — ingest is TBD

(Note: the table was renamed to `marketing_leads_daily_actuals` by
`2026-04-21_rename_marketing_tables.sql`. The discussion below uses the
current name; source names of Looker views etc. haven't changed.)

Three candidate paths, in rough increasing order of effort and correctness:

1. **Derive from `leads_weekly_actuals` (cheapest, least accurate).**
   Split each weekly row by a fixed daily weighting (day-of-week factors)
   into seven daily rows and mark `source='derived:leads_weekly_actuals'`.
   Good enough for smoke-testing dashboards and
   `marketing_forecast_vs_actual_daily` but introduces a fake daily curve.
   Paid/organic split is impossible from weekly data alone unless the weekly
   table already carries it.

2. **New Looker schedule (middle effort, correct grain).**
   Add a second Looker webhook that delivers daily × BU × lead_source ×
   audience × paid_channel rows to a new Edge Function (or extend the
   existing `bright-worker`). Writes directly to `marketing_leads_daily_actuals`.
   Blocked today by the same org-permission issue as the
   Supabase-triggers-GitHub work; unblocking one unblocks both.

3. **Direct API pull (most effort, most flexibility).**
   Python script runs daily, queries Google Ads + Salesforce (or whatever
   the underlying sources are) directly, writes to
   `marketing_leads_daily_actuals`. Bypasses Looker entirely — useful if
   Looker becomes a bottleneck or if we need same-day freshness that Looker's
   batch cadence can't provide.

Recommendation once permissions are unblocked: option 2. It reuses the
Edge Function infrastructure that already exists for
`leads_weekly_actuals`, has the right grain natively, and doesn't require
maintaining a second integration surface.
