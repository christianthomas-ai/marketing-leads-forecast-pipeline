# Marketing Forecasting Modernization — Project Manifest

## Purpose of this document
This is a working manifest for a multi-phase project to modernize a weekly leads forecasting model. It captures architectural decisions, current state, open questions, and a sequenced roadmap. Intended for use as persistent context in Cursor.

See also: [`claude-md.md`](./claude-md.md) for the current-state architecture reference (tables, Edge Function behavior, timing chain, known gotchas).

---

## 1. Current state

### Existing architecture
- **Control layer:** Google Sheets. Houses weight inputs, manual overrides, interactive recalc for scenario testing, Monday morning review view, and the published forecast sent to planning.
- **Automation:** Apps Script time-driven trigger at 6 AM CT Mondays force-recalculates the Sheets model before 9 AM publish deadline.
- **Backend (in progress):** Python + Supabase (PostgreSQL) + Google Cloud Functions. Scoped initially to pre-computing trend inputs pushed as clean summary tables into Sheets.

### Forecasting methodology
- Blends two signals: 4-week trailing YoY trend and 3-year WoW trend.
- Applied to prior week actuals (or prior forecasts, when cascading) to generate a 52-week rolling top-line forecast.
- Cascades into channel/lead source allocation and audience allocation layers.
- Nominal lead adjustments rebalance proportionally to preserve top-line reconciliation.
- Manual overrides handle: holidays, spring break drift, tROAS changes, reporting changes, undocumented promotions.

### Business units
- VT Core N.A.
- International
- Prof Certs

### Key design constraint
Weight adjustments must produce instant feedback in Sheets. Python is not used for the live recalc — only for pre-computing inputs and (going forward) backend storage/analytics.

### Memorialization today
- 52-week forecast is memorialized in Google Sheets.
- Grain: daily × business unit × lead channel.
- Captures: leads forecast and ad spend forecast.

---

## 2. Architectural decision: shift weight to Supabase

The memorialization + base-vs-final adjustment logging requirements push the architecture beyond what Sheets can handle as the system of record. Decision:

- **Supabase = system of record.** Historical actuals, every forecast vintage at full grain, adjustment log (holiday calendar, tROAS targets, promo flags, reporting changes), base-vs-final decomposition, audit trail.
- **Python = heavy lifting.** Base model computation, sequential adjustment application, writing staged outputs to Supabase with per-stage deltas for attribution.
- **Sheets = control surface and weekly workspace.** Pulls inputs from Supabase, pushes published forecast back to Supabase, retains interactive override layer.

### Migration principle
Build the Python engine in parallel with the existing Sheets model for 4–6 weeks. Reconcile outputs weekly. Only cut over when Python matches Sheets to within rounding. Then Sheets becomes UI on top, not the engine.

---

## 3. Supabase data model (target state)

### Core tables

**`actuals`**
- Grain: `date × business × lead_source`
- Fields: `date`, `business`, `lead_source`, `paid_leads`, `total_leads`, `organic_leads` (computed: total − paid), `split_source` (enum: `measured_from_ppc_explorer`, `entirely_paid`, `entirely_organic`, `estimated`), `ad_spend`
- `split_source` enables weighting observations by split quality in downstream models.

**`week_metadata`**
- Per-week flags: holiday presence, anomalies, reporting change flags.

**`marketing_calendar`** *(originally sketched as `holiday_calendar`; landed 2026-04-22 as a full dense date dimension rather than sparse-events-only — see `supabase/migrations/2026-04-22_marketing_calendar.sql` and `MODEL_STUDY_NOTES.md` §10 for the design rationale. The table co-locates event info with calendar primitives: one row per date 2022-2027, with event/classifier fields sparse and week-rollup fields dense.)*
- Grain: one row per date (~2,190 rows for 6 years).
- Event fields (sparse): `event_name`, `event_type` (holiday/test_release), `same_day_of_week`, `same_date`, `full_week_impact`.
- Week rollup (dense, derived in loader): `week_event_name`, `week_has_full_week_impact`.
- Loader: `load_calendar.py` (reads Sheet → upserts with soft-supersede on change).
- **Deferred to Phase 3:** per-BU `expected_leads_multiplier` weightings. Those will land as a child table (`marketing_holiday_weightings`) once `marketing_leads_daily_actuals` is populated and we can derive weights from historical actuals rather than guess them.

**`troas_targets`**
- Fields: `effective_date`, `business`, `channel`, `troas_target`, `prior_troas` (for delta computation).
- Applies to paid search channels: Search Tutor PPC, Search Non Tutor (to confirm).

**`eltv_change_log`**
- Fields: `effective_date`, `subject`, `prior_methodology`, `new_methodology`, `notes`.
- Critical because Google Smart Bidding optimizes against values sent, not true LTV. eLTV methodology changes alter Google's behavior independent of tROAS changes.

**`promo_log`**
- Documented and undocumented promos with start/end dates and affected BUs/channels.

**`google_campaign_lead_source_mapping`**
- Fields: `google_campaign_id`, `lead_source`, `allocation_pct`, `effective_date`, `end_date`.
- Makes the imperfect mapping between Google's campaign taxonomy and internal lead source taxonomy explicit and auditable.

### Forecast vintage table

**`marketing_forecast_vintages`** *(originally sketched as `forecast_vintages`; renamed 2026-04-21 for domain namespacing. The actual shipped schema uses plan_type x plan_start_date x forecast_date x business x lead_source x audience — see `supabase/migrations/2026-04-20_forecast_vintages.sql` for the real definition. The grain below is the older design sketch, kept for historical context.)*
- Grain: `vintage_date × forecast_date × business × lead_channel × forecast_type`
- Fields:
  - `vintage_date` (indexed)
  - `forecast_date` (indexed)
  - `business`
  - `lead_channel`
  - `forecast_type` (enum: `engine`, `published`; later possibly per-stage: `base`, `+allocation`, `+holiday`, `+troas`, `+manual`, `final`)
  - `leads_forecast`
  - `spend_forecast`
  - `model_version`
  - `notes` (freeform)
  - `published_by`
  - `published_at`
- Upsert: `ON CONFLICT (vintage_date, forecast_date, business, lead_channel, forecast_type) DO UPDATE`.
- Consider soft-overwrite with `is_current` boolean if full audit trail of re-publishes is needed.

### Row volume sanity check
3 BUs × ~8 channels × 365 days × weekly vintages ≈ 8,700 rows per vintage ≈ 450K rows/year. Fine for Postgres. Index on `vintage_date` and `forecast_date` from day one.

---

## 4. Base-vs-final adjustment decomposition

The Python engine should compute and store the forecast at each stage:

1. `base` — trend only (trailing YoY × WoW)
2. `+allocation` — after channel/audience mix applied
3. `+holiday` — after holiday adjustments
4. `+troas` — after tROAS response model adjustments
5. `+manual` — after any Christian overrides
6. `final` — published

Each stage stored at daily × BU × channel grain. Delta between stages = attribution. This turns "leads down 8% WoW" into "5pts tROAS, 2pts holiday, 1pt trend, 0pts manual" automatically.

---

## 5. Write path for published forecast

### Decision
Python job triggered manually after Monday morning review. Reads Sheets via Google Sheets API, reshapes to long format, validates, upserts to Supabase.

### Validation layer (runs before write)
- **Shape check:** expected row count = `days_in_horizon × BUs × channels`. Abort if off.
- **Null check:** no nulls in forecast value columns. Abort if any.
- **Reconciliation check:** sum of channel forecasts within a BU = BU forecast; sum of BU forecasts = top-line. Abort on mismatch beyond tolerance.
- **Sanity check:** flag any daily forecast >3x or <0.3x trailing 4-week average. Warn, don't abort.
- **Duplicate check:** block if `published` vintage already exists for today unless `overwrite=True` flag is passed.

### Reconciliation safeguard
After successful write, script writes back to a `last_published_vintage` range in Sheets with: `vintage_date`, `rows_written`, `total_top_line_leads_Q3`. Glance-check against published view before closing out.

### Trigger sequencing
1. Manual Python command for first 2–4 weeks alongside normal publish flow.
2. Compare Supabase contents to what was sent to planning, fix discrepancies.
3. Once reliable, either keep manual or wire to Sheets menu button via Apps Script → Cloud Function.
4. Add engine-output write (`forecast_type='engine'`) once published path is stable.

---

## 6. tROAS response model (marketing forecasting deep work)

### Reframing
Goal is NOT to replicate Google Smart Bidding (impossible — proprietary deep learning over ~70+ auction signals against unseen competitors). Goal IS to learn the elasticity curve: how tROAS changes at our business propagate to spend, CPL, and leads, with known lags and saturation.

### Data requirements
- **Google Ads API access:** campaign-level daily data on spend, impressions, clicks, conversions, conversion value, CPC, impression share, lost IS (budget), lost IS (rank), tROAS target active each day.
- **Historical tROAS change log:** every change with effective date, campaign/group, prior value.
- **eLTV change history:** already have — log as events.
- **Downstream conversion data:** leads, sCVR by cohort, actual LTV realization vs eLTV sent.

### Event study methodology
For every historical tROAS change, build a panel: −28 to +28 days around the change, daily, for affected campaigns + control set (same BU, similar seasonality, no tROAS change in window). Capture: spend, impressions, clicks, CPC, conversions, conversion value, impression share, leads.

Purpose: empirically fit response curves, not train a neural net. Small-data problem. Use difference-in-differences to separate tROAS effect from holiday/seasonality confounds.

### Chain to model
tROAS target → bid behavior → auction win rate & CPC → spend & impressions → clicks → conversions (leads) → eventual value.

- **tROAS → spend:** elasticity per campaign group, conditioned on impression share and budget headroom. Strong nonlinearity when IS >90% (supply-capped) vs <50% with high lost-IS-to-rank.
- **spend → clicks:** roughly linear at the margin, slowly rising CPC.
- **clicks → leads:** CVR as slowly-moving baseline with variance.
- **leads → conversion value → Smart Bidding learning loop:** 2–3 week lag after material eLTV methodology changes.

### Key effects to model
- Learning period (~7–14 days after material changes; flag and weight or exclude).
- Saturation / diminishing returns (concave functional form).
- Seasonality and demand (normalize by category demand; Google Trends as free proxy).
- Portfolio vs campaign-level bid strategies (model at bid strategy level).
- Competitive dynamics (via auction insights: overlap rate, outranking share, lost-IS-to-rank).
- eLTV drift (track realized value vs sent eLTV as diagnostic).

### Integration into forecast engine
- Baseline forecast holds current tROAS constant.
- Planned tROAS change → response model outputs delta curve (spend, CPL, leads) for next 28–56 days.
- Delta applied as adjustment layer in engine, same architecture as holiday and promo adjustments.
- Memorialize pre- and post-tROAS-adjustment forecasts to measure response model accuracy.

---

## 7. Handling attribution complications

### Holiday normalization
- Include holiday indicators (or holiday-proximity index for multi-day drift) as controls in event study regressions.
- When tROAS change coincides with holiday window: flag, weight lower, or exclude from v1 fit. Use difference-in-differences with control campaigns.
- Single holiday calendar in Supabase is prerequisite; every downstream model reads from it.

### Mixed paid/organic lead source (brand/direct)
- **Split methodology:** PPC explorer provides paid portion; all-business KPIs explore provides total; organic = total − paid.
- **Validation before trusting subtraction:**
  - For entirely-paid sources, PPC explorer count should equal KPIs count day-by-day.
  - For entirely-organic sources, PPC explorer should show zero.
  - Sum of paid leads across all sources should reconcile to any existing "total paid" rollup.
- **Wrinkles to manage:**
  - Attribution lag mismatch between explores → pull both with 3–7 day lag; only trust settled data.
  - Definitional scope differences (e.g., PPC explorer is Google-only but brand/direct may aggregate branded paid across platforms) → verify scope.
  - Historical reprocessing → log methodology changes as events.
  - Negative organic counts → floor at zero for analysis, log occurrences; frequent negatives = definitional issue.
- **Conversation to have:** Sit with owner of PPC explorer + KPIs explore. Ask directly: "if I subtract paid from total for brand/direct, am I getting what I think I'm getting?" Validates definitions and surfaces edge cases.

### Downstream analytical opportunity
With clean split, can test: does paid brand activity cannibalize or lift organic brand? Measurable via tROAS change events on branded campaigns → response in organic series. Rare dataset, strategic value.

---

## 8. Accuracy tracking and feedback loop

- Every forecast vintage logged at full grain.
- Weekly accuracy dashboard: forecast vs actual by BU, MAPE over time, error decomposition (trend miss, tROAS miss, holiday miss, unexplained).
- Earns the right to push back on business partners with data ("your tROAS change landed 40% softer than forecast three times in a row").
- Response model specifically: memorialize predictions vs actuals for every future tROAS change; iterate quarterly.

---

## 9. Sequenced roadmap

### Phase 1 — Foundation (next)
1. Validate memorialization snapshot trigger approach (Python job at ~7 AM CT Mondays, after Apps Script recalc, before 9 AM arrival).
2. Settle `marketing_forecast_vintages` schema with validation layer.
3. Build Python write function: Sheets API → pandas → Supabase upsert with validation.
4. Run manually 2–4 weeks alongside existing publish; reconcile.
5. Stand up `actuals` table in Supabase with daily × BU × lead_source × paid/organic split.

### Phase 2 — Adjustment infrastructure
6. **[2026-04-22, landed]** Build `marketing_calendar` in Supabase (dense date dimension + event overlay); load 2022-2027 from the Sheet's Calendar tab. Loader at `load_calendar.py`. Per-BU holiday weightings deferred to Phase 3 (derived from actuals, not guessed).
7. Build `troas_targets` table with effective-date history.
8. Build `eltv_change_log` and `promo_log` tables.
9. Build `google_campaign_lead_source_mapping` table.
10. Google Sheets → Supabase push script for tROAS tables (calendar already has one).

### Phase 3 — Python forecast engine
11. Implement base model in Python (trend + allocation only, no adjustments). Reconcile against Sheets.
12. Add adjustment layers sequentially: holiday → tROAS → manual. Each stage snapshots to Supabase.
13. Refactor Sheets to pull trend inputs and adjustment tables from Supabase.
14. Sheets retains interactive override; final state pushed back to Supabase on publish.

### Phase 4 — tROAS response model
15. Secure Google Ads API access; daily campaign-level pull to Supabase.
16. Reconstruct tROAS change history and eLTV change history as event logs.
17. Validate brand/direct paid/organic split against PPC explorer + KPIs explore.
18. Build event study dataset (SQL-heavy, no ML needed for v1).
19. Fit v1 elasticities per BU × campaign type, with holiday controls and impression share as conditioning variable.
20. Wire v1 as tROAS adjustment layer in forecast engine.
21. Memorialize response model predictions vs actuals; iterate.

### Phase 5 — Analytics and loop-closing
22. Weekly accuracy dashboard with error decomposition.
23. Scenario engine ("what if we raise Search Tutor PPC tROAS by 15% next week?").
24. Cross-BU pattern detection (early warning reads for CMO/VP Marketing).
25. Brand paid → organic cannibalization/lift study.

---

## 10. Operational constraints

- **Monday 9 AM CT deadline** for all automated pipeline outputs.
- **Apps Script trigger** at 6 AM CT Mondays force-recalculates Sheets before arrival.
- **Central time, ~9–5 working hours.**
- **Publish timing:** published forecast reflects post-review state; engine output reflects pre-review state. Both should be stored separately when possible.

---

## 11. Open questions / to confirm

- Confirm exact paid search channels affected by tROAS targets (Search Tutor PPC confirmed; Search Non Tutor to confirm).
- Confirm attribution lag for PPC explorer vs all-business KPIs explore (needed to set the "don't trust data younger than X days" threshold).
- Confirm whether PPC explorer's "paid" definition is Google-only or cross-platform.
- Decide: Python write triggered by manual command vs Sheets menu button vs post-publish webhook.
- Decide: hard overwrite on re-publish vs soft overwrite with `is_current` audit trail.

---

## 12. Terminology glossary (for Cursor context)

MRR, CAC, tROAS (target ROAS), sCVR (session conversion rate), ARPM, LookML, YoY, WoW, YTG, CTG, run rate, 6+6 forecast, S&OP, Meta/Non-Meta attribution, IMPORTRANGE, CPL, eLTV, impression share (IS), lost IS to rank, lost IS to budget.
