# Marketing Model — Deep Study Notes

Living document built by reading the Marketing Model - Live spreadsheet and the
Python/Supabase pipeline tab-by-tab, file-by-file. Treats the model as the CPA
exam: ground understanding in source material, not guesswork.

Pairs with:
- `PROJECT_MANIFEST.md` (target-state architecture, forward-looking)
- `claude-md.md` (current pipeline state)
- `PHASE1_HANDOFF.md` (Phase 1 status, locks + reconciliation)

Last updated: 2026-04-21

---

## 0. Executive summary

The marketing forecast is a **5-stage pipeline** that flows from raw Looker
actuals through Python/Supabase forecasting into a large Google Sheet that
humans tweak. The final lock lives in Supabase (`marketing_forecast_vintages`).

```
┌─────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│ Looker          │    │ Supabase         │    │ Google Sheet     │
│ (BI schedule)   │───▶│ (actuals+Python  │───▶│ (41 tabs,        │
│ 4:30 AM CT      │    │  forecast RPC)   │    │  formula engine) │
└─────────────────┘    └──────────────────┘    └──────────────────┘
                                                       │
                                                       ▼
                                       ┌───────────────────────────────┐
                                       │ Human adjustments:            │
                                       │   • Week Adj (Top Line col U) │
                                       │   • Carry Forward Adj (col V) │
                                       │   • Fcst Adj (col AK)         │
                                       │   • CY Bias (Forecast Adj     │
                                       │     Step 1 / Step 2)          │
                                       │   • Initiatives toggle        │
                                       └───────────────────────────────┘
                                                       │
                                                       ▼
                                              ┌──────────────────┐
                                              │ Daily Forecast - │
                                              │ LOCKED archive   │
                                              │ (paste values)   │
                                              └──────────────────┘
                                                       │
                                                       ▼
                                              ┌──────────────────┐
                                              │ marketing_       │
                                              │ forecast_        │
                                              │ vintages         │
                                              └──────────────────┘
```

**Key unlock for Phase 2 (holiday calendar):** the Python forecast today is NOT
holiday-aware. Holidays drift year-over-year (Easter was in a different week in
2025 than 2026), which corrupts `trailing_4wk_yoy` and feeds bad numbers into
`baseline_forecast`. Today the human patches this by typing `-0.10` into Top
Line column U (Week Adj). A Supabase `marketing_holiday_calendar` lets us
automate that patch by making the Python forecast (or a pre/post adjustment
layer) aware of full-week holidays.

---

## 1. The 41 tabs, grouped by function

```
To do                         Scratch
Control                      📎  Config hub (named ranges, lookup tables, weights)
Calendar                     📎  Date dimension (2022-2027, holidays, events)

=== Reporting ===            (Outputs for humans)
  Base vs final OP             Visual comparisons
  Weekly - Totals              Executive weekly summary (the PoP / YoY grid)
  Monthly - Totals             Executive monthly summary
  Weekly Summary_Audience      Audience rollup

=== Forecasts ===            (Wide grids of forecast values)
  Top-Down Daily Forecast    📎  MAIN ENGINE. 498 x 1112. Weekly totals × daily shares.
  Top-Down Weekly Forecast     Weekly rollup of Daily
  Top-Down Monthly Forecast    Monthly rollup

=== Mix Adjustments ===      (Apply user bias per week)
  Adjustment Charts            Visual
  Forecast Adj Step 1: Source  CY Bias multiplier by (BU × lead_source × week)
  Forecast Adj Step 2: Audience CY Bias multiplier by (BU × audience × week)

=== Inputs ===               (Human-entered config)
  Top Line                   📎  Per-BU weekly total. Baseline + manual adj + initiatives.
  Meta Leads                    Meta-specific adjustments
  Same Day of Week Holidays - Weekly Allocation   Daily shares for MLK/Memorial/etc.
  Same Date Holidays - Weekly Allocation          Daily shares for Jan 1 / Jul 4 / Dec 25 / etc.
  Test Prep Score Release Impacts                 SAT/ACT release impact
  CPNL Forecast                 Cost per net lead
  Initiatives                   Promotional overlays (toggleable on/off)

=== Models ===               (Computation layers)
  Daily - Forecast              
  Weekly - Final Forecast       Last stage
  Weekly - Source Adjusted Forecast  Step 2 (after Source mix adj)
  Weekly - Baseline Forecast    Step 1 (pulls Top Line total, disaggregates by mix)
  ROAS                          Return on ad spend
  ROAS Assumption               Assumption inputs

=== Data ===                 (Data sources / sinks)
  Supabase Forecast          📎  Python pipeline output (Business × week totals)
  Copy of Supabase Forecast     Backup
  KPIs (All Businesses)      📎  Looker IMPORTRANGE pipe (detailed actuals)
  PPC (Full Funnel)             Full-funnel PPC data
  Historical ROAS               Past ROAS for YoY

=== Prior Plan ===           (Archives)
  Top-Down Tabular           📎  24,823-row long-form output (all plan history in sheet)
  Daily Forecast - LOCKED For Summary Tables    📎  Paste-values archive → Supabase
  Copy of Daily Forecast - LOCKED For Summary Tables   Backup
```

---

## 2. `Control` tab — the config hub

**Left block (rows 1–25, cols A–L): anchors + run-rate weights**

| Cell | Name | Value / Formula |
|---|---|---|
| `I5` | Model Anchor Date | `46131` (= 2026-04-26, current SOP start) |
| `I6` | Week Zero SOP | `=INDEX(Calendar!E:E, MATCH(I5, Calendar!D:D, 0))` |
| `I7` | OP Plan Short Name | `Q2OP` |
| `I8` | OP Plan | `_2026W1` |
| `I9` | Week Zero OP Date | `46082` (= 2026-03-08) |
| `I10` | OP Plan Start Week | lookup |
| `I12` | Run-Rate Weeks | `4` |
| `H14:I22` | Run-Rate weighting grid | Week # / weight table. Currently equal 0.25 weights across 4 most-recent weeks. |

**Right block: lookup tables** — critical for understanding the domain:

| Range | Purpose |
|---|---|
| `M:O` | Audience → Super Group mapping (Col-STEM → College & Grad → CG, etc.) |
| `Q:R` | Impacted Lead Source (All PPC / Brand & Direct / Phone / etc.) |
| `S:T` | Model Lead Source (canonical names) |
| `U` | Business list (Total / VT Core / International / Prof Certs / VT Core N.A.) |
| `W` | Audience list (Total + 13 audiences) |
| `Y` | Scenario (Baseline / Final) |
| `AE:AF` | Month → Quarter mapping |
| `AI:AJ` | Day of Week → number (Sunday=0, ..., Saturday=6) |
| `AI:AK` | **Looker Lead Source → Model Lead Source → rollup group** (Brand/Other, Search, Meta, eCommerce) |

**Looker → Model lead source mapping (critical):**

| Looker name | Model name | Rollup |
|---|---|---|
| Brand & Direct | Brand & Direct | Brand/Other |
| Other | Other | Brand/Other |
| Phone | Phone | Brand/Other |
| Search - Tutor PPC | Search (Tutor) | Search (Tutor) |
| Search - Tutor SEO | Search (Tutor SEO) | Search (Tutor) |
| Search - Non Tutor | Search (Non Tutor) | Search (Non-Tutor) |
| Facebook | Meta - Phone | Meta - Phone |
| Meta - eComm | Meta - eComm | eCommerce |
| Search - Bing | Search - Bing | — |
| Meta - Phone | Meta - Phone | — |

→ The Supabase `marketing_forecast_vintages` table uses **Looker names** for
`lead_source` (Search - Tutor PPC, Search - Tutor SEO split), not Model names.
This is already handled correctly in `vintage_validation.KNOWN_LEAD_SOURCES`.

---

## 3. `Calendar` tab — the date dimension

**Shape:** 2,206 rows × 48 cols, one row per date from ~2022-01-02 through
~2027-12-31 (bleeds into 2028).

### 3.1 Columns B–Z (per-date attributes)

| Col | Name | Meaning |
|---|---|---|
| B | Date | `YYYY-MM-DD` |
| C | Day of Week | `TEXT(B, "dddd")` |
| D | Week Start | Sunday of that week |
| E | Week Number | Sequential integer, aligned to Control anchor |
| F | Relative Week | `(Week Start − anchor) / 7` |
| G | Week Tag | `_YYYYW##` formatted |
| H | Week Year | `YEAR(Week Start)` |
| **I** | **Holiday Input** | **User-entered holiday name on the holiday date.** Easter on Easter Sunday; "Holy Week" on Palm Sunday (single cell, fan-out via formulas). |
| J | Holiday Week Flag | `SUMPRODUCT(...)` — TRUE if any date in this week has col I populated |
| K | Holiday Week Name | `INDEX/FILTER` — returns col I value from the week |
| L–Q | Same DOW / Same Date / Full Week classifiers | VLOOKUPs against the `AI:AM` attribute table |
| R | ACT Test Score Release | Flag |
| S | SAT Test Score Release | Flag |
| T–Z | Test Prep release-related derived cols | Date/week/impact |

### 3.2 Columns AI:AM — holiday attribute table (the core)

**Confirmed contents:**

| AI (Holiday)     | AJ (Same DOW) | AK (Same Date) | AL (Full Week) | AM (year count) |
|---|---|---|---|---|
| New Year's Day   | FALSE | TRUE  | FALSE | COUNTIFS |
| MLK Day          | TRUE  | FALSE | FALSE | COUNTIFS |
| Presidents' Day  | TRUE  | FALSE | FALSE | COUNTIFS |
| Memorial Day     | TRUE  | FALSE | FALSE | COUNTIFS |
| Juneteenth       | FALSE | TRUE  | FALSE | COUNTIFS |
| Independence Day | FALSE | TRUE  | FALSE | COUNTIFS |
| Labor Day        | TRUE  | FALSE | FALSE | COUNTIFS |
| Columbus Day     | TRUE  | FALSE | FALSE | COUNTIFS |
| Veterans Day     | FALSE | TRUE  | FALSE | COUNTIFS |
| Thanksgiving Day | TRUE  | FALSE | **TRUE**  | COUNTIFS |
| Christmas Day    | FALSE | TRUE  | **TRUE**  | COUNTIFS |
| New Year's Eve   | FALSE | TRUE  | FALSE | COUNTIFS |
| Christmas Eve    | FALSE | TRUE  | FALSE | COUNTIFS |
| Spring Break 1   | FALSE | FALSE | **TRUE**  | COUNTIFS |
| Spring Break 2   | FALSE | FALSE | **TRUE**  | COUNTIFS |
| Holy Week        | FALSE | FALSE | **TRUE**  | COUNTIFS |
| Easter           | TRUE  | (FALSE*) | FALSE | COUNTIFS |
| SAT              | FALSE | FALSE | **TRUE**  | COUNTIFS |
| ACT              | FALSE | FALSE | **TRUE**  | COUNTIFS |

*Easter's AK is `=IF(AJ19=TRUE, FALSE, TRUE)` — since AJ19=TRUE, AK evaluates to FALSE.

**Classification logic:** AK = "not same day of week" = TRUE (so FALSE if AJ=TRUE). AL = "neither" = TRUE (so FALSE if either AJ or AK). Except Thanksgiving / Christmas / Spring Break / Holy Week / SAT / ACT which have AL hard-coded TRUE.

### 3.3 Full-Week Impact holidays (the ones the Supabase calendar cares about)

**These are the holidays where the ENTIRE WEEK's top-line leads shift materially
vs a non-holiday week:**

1. **Thanksgiving** (always same week-of-year in US; low top-line impact ambiguity)
2. **Christmas** (fixed date; shifts weekdays yearly)
3. **Spring Break 1** (variable date, user-entered)
4. **Spring Break 2** (variable date, user-entered)
5. **Holy Week** (variable, tied to Easter; user enters on Palm Sunday)
6. **SAT release** (variable; user-entered)
7. **ACT release** (variable; user-entered)

Of these, the **variable-date ones (Spring Break 1/2, Holy Week, SAT, ACT)** are
the problem for YoY comparison because Week 15 in 2026 might be Holy Week but
Week 15 in 2025 might be a normal week → the trailing_4wk_yoy calc in Python
mixes them up and the human has to patch manually.

---

## 4. `KPIs (All Businesses)` tab — Looker IMPORTRANGE pipe

**Shape:** 207,458 rows × 17 cols. Not native data — an IMPORTRANGE pipe.

```
A1: "Marketing Model Looker Data"   (label)
A2: <Looker look URL>                (source)
Row 2: per-column range refs (pages 1)
Row 3: per-column range refs (page 2)
Row 4: =ARRAYFORMULA({IMPORTRANGE(...); IMPORTRANGE(...)})
```

**Schema (inferred from Weekly - Baseline Forecast formulas):**

| Col | Name | Used by |
|---|---|---|
| A | (Date / ingest timestamp) | freshness filter in Top Line formulas |
| B | week_start | SUMIFS key |
| C | Business | SUMIFS key |
| D | audience | SUMIFS key |
| G | leads | summed value |
| H | lead_source | SUMIFS key |
| N | ? | |

**⚠️ Reading trap:** any render option other than `formula` triggers IMPORTRANGE
resolution → consistently >3 min → Google API 503. Read via `--render formula`
only. For computed values, read from Supabase (the upstream source).

→ The Supabase migration path for actuals should pull from Looker directly
(via a new webhook), bypassing this tab. The tab stays as a legacy dependency
for Sheet formulas until the full Python rewrite.

---

## 5. The Python / Supabase forecast pipeline

**Upstream (Supabase, not in this repo):**
1. Looker webhook (4:30 AM CT) → Edge Function `bright-worker`
2. Edge Function wipes + reloads `leads_weekly_actuals` in Supabase
3. Edge Function calls `generate_forecast()` RPC → populates `leads_forecast` table

**This repo's role:** `push_to_sheets.py`
1. **Step 0** (`check_actuals_freshness`): abort if `leads_weekly_actuals.Reporting Date`
   is >3 days old (catches failed webhook / silent Edge Function error)
2. **Step 1** (`run_forecast`): retry `generate_forecast()` via RPC (defensive)
3. **Step 2** (`fetch_forecast`): pull `leads_forecast` via PostgREST
4. **Step 3** (`push_to_sheets`): clear + write to `Supabase Forecast` tab

### 5.1 `Supabase Forecast` tab schema

| Col | Name | Meaning |
|---|---|---|
| A | Business | `International` / `VT Core` / `Prof Certs` |
| B | week_year | e.g. 2022 |
| C | week_number | 1–53 |
| D | week_start | `YYYY-MM-DD`, key for joins |
| E | is_actual | `True` / `False` |
| F | actual_leads | populated when is_actual=TRUE |
| G | py_leads | prior-year same week |
| H | prior_week_forecast | previous week's forecast, chained |
| I | avg_wow_3yr | 3-year average WoW growth rate |
| J | trailing_4wk_yoy | 4-week trailing YoY growth rate |
| **K** | **baseline_forecast** | **= actuals (if actual) else forecast. The key column.** |
| L | forecast_adj | forecast-level adjustment (usually 0) |
| M | final_forecast | K × (1 + L) |

→ `Top Line` and other downstream tabs key off **column K** via
`=sumifs('Supabase Forecast'!$K:$K, 'Supabase Forecast'!$D:$D, week_start, 'Supabase Forecast'!$A:$A, Business)`.

**⚠️ Critical gap:** `baseline_forecast` is computed from `avg_wow_3yr` and
`trailing_4wk_yoy` (SQL in Supabase, not in this repo). There is **no
holiday-awareness**. When Easter drifts one week year-over-year, the
trailing_4wk_yoy picks up a bad signal and propagates into the forecast.

---

## 6. `Top Line` tab — the per-BU weekly total (with human knobs)

**Shape:** 442 × 52. One row per (Business × week).

### 6.1 Column layout (per row `n`)

| Col | Header | Formula | Role |
|---|---|---|---|
| B | Business | | plan key |
| C | Scenario | `=IF(E>=B1, "Forecast", "Actual")` | dynamic; `B1` = anchor cutover |
| D | Week # | `VLOOKUP(E, Calendar!D:E, 2)` | | 
| E | Week | `=E_(n+1)+7` | walks backward in time from row 4 |
| F | CY | `SUMIFS('Supabase Forecast'!K, D, E, A, B)` | current year baseline from Supabase |
| G | PY | `SUMIFS('Supabase Forecast'!K, D, E-52*7, A, B)` | prior year same week |
| H | YoY | `F/G` | |
| J | CY WoW | `F/F_next` | |
| K | PY WoW | `G/G_next` | |
| **Topline Forecast Provided section (M:O)** | | | **Override input** |
| M | Week # | `=D` | |
| N | 2026 | (user input; blank default) | optional manual forecast override |
| O | YoY | `IF(N="", "", N/G)` | |
| **Final Baseline section (Q:V)** | | | **Where Supabase + human combine** |
| Q | Week # | `=D` | |
| **R** | **CY** | `IF(C="Actual", SUMIFS(Supabase K, ...), SUMIFS(Supabase K, ...) * (1 + T))` | **CY baseline × (1 + Week Adj)** |
| S | YoY | `R/G` | |
| **T** | **Total Adjustment** | `=SUM(U, SUM(V:V$108))` | **= Week Adj + all Carry Forward Adjs for this & later rows** |
| **U** | **Week Adj** | (user input, usually blank) | **Per-week bump. Row 56 has `-0.1`, Row 57 has `-0.03`.** |
| **V** | **Carry Forward Adj** | (user input, usually blank) | **Persistent bump applied from this row onward. Row 60 has `-0.15`.** |
| X | ROAS CY | `SUMIFS(Historical ROAS, E, ...)` | reference |
| Y | ROAS PY | `SUMIFS(Historical ROAS, E-52*7, ...)` | reference |
| AA | Holiday CY | `VLOOKUP(E, Calendar!D:Z, col-K)` | → Calendar col K (Holiday Week Name) |
| AB | Test Prep Impact CY | `VLOOKUP(E, Calendar!D:Z, col-Z)` | → Calendar col Z (test prep impact) |
| AD | Holiday PY | same, E-52*7 | |
| AE | Test Prep Impact PY | same, E-52*7 | |
| AF | Comment | (user text) | |
| **Final Forecast w/ Initiatives (AH:AL)** | | | **True final forecast** |
| AH | Week # | `=D` | |
| **AI** | **CY** | **`(R + AP) * (1 + AK)`** | **Final = (Baseline + Initiatives) × (1 + Fcst Adj)** |
| AJ | YoY | `AI/G` | |
| **AK** | **Fcst Adj** | (user input, usually blank) | **Final-level bump (not carried forward)** |
| AL | Vs Final Baseline | `AI/R` | |
| AP | 2026 (Initiatives) | `IF(AP$1="On", SUMIFS(Initiatives!$287, $3, D), 0)` | **Initiatives toggle in AP1** |

### 6.2 The key formula: Final Forecast = (Baseline + Initiatives) × (1 + Fcst Adj)

Where **Baseline** = `Supabase Forecast!K × (1 + Week Adj + cumulative Carry Forward)`.

**The three human knobs for holiday correction:**
1. **Column U (Week Adj)**: one-shot bump for a specific week.
   *Observed values in the live model:* `U56 = -0.10`, `U57 = -0.03`. These
   are very likely the Easter/Holy Week corrections the user mentioned.
2. **Column V (Carry Forward Adj)**: persistent bump from this week forward.
   *Observed value:* `V60 = -0.15` (a longer-term run-rate tilt).
3. **Column AK (Fcst Adj)**: final bump after Initiatives added.

### 6.3 Downstream consumption of `Top Line!AI`

The `Weekly - Baseline Forecast` tab takes `Top Line!AI` (per-BU-per-week total)
and **disaggregates it to (BU × lead_source × audience × week)** via the mix
weight in its own column `$DH` (historical contribution share). See §7.

---

## 7. `Weekly - Baseline Forecast` — disaggregation layer

**Shape:** 649 × 275. The formula engine that splits Top Line totals into rows.

**Row structure:**
- Rows 1–2: section headers ("PPC")
- Rows 3–15: summary block (per BU × lead_source_group, SUMIFS into data block)
- Rows 16–22: reporting-week metadata (`Reporting Week` row 20, Reporting Day row 21, Day of Week row 22, `Scenario` row 16, `Leads (Valid)` headers row 23)
- **Rows 24–255: data grid.** Each row = one (Business × Lead Source × Audience) combination.

**Core data cell formula:**

```excel
=IF(E$16="Actual",
    SUMIFS('KPIs (All Businesses)'!$G:$G,
           'KPIs (All Businesses)'!$B:$B, E$20,       -- week_start
           'KPIs (All Businesses)'!$C:$C, $B24,       -- Business
           'KPIs (All Businesses)'!$D:$D, $D24,       -- audience
           'KPIs (All Businesses)'!$H:$H, $C24),      -- lead_source
    IF(E$16="Forecast",
       SUMIFS('Top Line'!$AI:$AI,                      -- final per-BU total
              'Top Line'!$E:$E, E$20,
              'Top Line'!$B:$B, $B24)
         * $DH24                                       -- × mix weight for THIS combo
         + IF(Initiatives!$K$3="On",
              IFERROR((INDEX(Initiatives!$M$286:$BE$331,
                             MATCH($C24, Initiatives!$K$286:$K$331, 0),
                             MATCH(E$20, Initiatives!$M$3:$BE$3, 0))
                        * $DI24), 0),
              0)
       )
)
```

Decoded:
- **Actuals branch:** pull actual leads from Looker pipe (KPIs tab).
- **Forecast branch:** take Top Line's final forecast for this week × this
  row's historical mix weight `$DH24`, then add Initiatives if toggled.

### 7.1 The mix weight `$DH24`

Lives at the far right of the Weekly - Baseline Forecast tab (col `DH`, row 24
onward). Formula:

```excel
=IFERROR(
  IF($B24="VT Core",
     SUMIFS($E24:$DF24, $E$17:$DF$17, DH$17) / SUMIFS($E$11:$DF$11, $E$17:$DF$17, DH$17),
     IF($B24="International",
        SUMIFS($E24:$DF24, ...) / SUMIFS($E$12:$DF$12, ...),
        IF($B24="Prof Certs",
           SUMIFS($E24:$DF24, ...) / SUMIFS($E$13:$DF$13, ...),
           0))),
  0)
```

Translation: **this row's historical lead share within its business**, summed
over the reference lookback window. Rows 11–13 are the BU totals
(VT Core / International / Prof Certs).

`$DI24` is the same idea but rolled up to lead_source-only (for Initiatives,
which are defined at that grain).

→ **Mix is historically-derived, not manually maintained.** New lead_source /
audience combos that appear in Looker automatically get mix shares.

### 7.2 `Weekly - Source Adjusted Forecast` → `Weekly - Final Forecast`

These apply `Forecast Adj Step 1: Source` (per BU × lead_source × week) and
`Forecast Adj Step 2: Audience` (per BU × audience × week) CY Bias multipliers.

**CY Bias example (Forecast Adj Step 1, row 3):**
```
1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0.9, =S3-0.05
```
→ 1.0 for most near-term weeks, then taper down in the back half
(0.9, 0.85, 0.80, ...). These are user inputs.

There's also a "Neg Check" row counting negative cells in the data block —
defensive validation.

---

## 8. `Top-Down Daily Forecast` — weekly-to-daily allocation

**Shape:** 498 × 1112. Too wide to read exhaustively; mechanism inferred from
adjacent tabs + formulas in Weekly Baseline.

**Logic:** for each (BU × lead_source × audience × date), the value is
`weekly_forecast × daily_weekday_share`, where the daily share comes from:

- `Same Day of Week Holidays - Weekly Allocation` — for weeks containing
  same-weekday holidays (MLK, Memorial, Labor, etc.), one daily share per
  weekday computed from historical actuals in those holiday weeks
- `Same Date Holidays - Weekly Allocation` — for same-date holidays (Jan 1,
  Jul 4, Dec 25), similarly
- Default weekday share for non-holiday weeks

→ Daily holiday handling is **already robust** and historically-grounded. Not
part of the Phase 2 Supabase calendar scope.

---

## 9. How holidays actually flow through today

```
User enters "Holy Week" in Calendar!I<date_of_palm_sunday>
    │
    ▼
Calendar!J<row> = TRUE (SUMPRODUCT fans it across the week)
Calendar!K<row> = "Holy Week" (INDEX/FILTER)
Calendar!L..Q  = classifier flags (via AI:AM lookup: FALSE, FALSE, TRUE)
    │
    ├───▶ Daily path (works):
    │     Same DOW/Date Holidays - Weekly Allocation
    │     → daily weekday shares → Top-Down Daily Forecast
    │
    └───▶ Top-line path (DOES NOT WORK automatically):
          Top Line!AA = "Holy Week"  (reference only, not an input)
          Top Line!AB = (test prep flag, reference only)
          Top Line!F  = CY from Supabase (baseline_forecast)
          Top Line!G  = PY from Supabase (same week prior year)
          
          ❌ If PY week was NOT Holy Week (Easter drifted), Top Line!G is
             from a normal week. Supabase baseline_forecast uses
             trailing_4wk_yoy which picks up this contamination.
          
          ✅ Today's fix: user manually types `-0.10` into Top Line!U<row>
             (Week Adj). Propagates through T → R → AI → Weekly Baseline.
```

**This is the gap the Supabase holiday calendar fills.** It lets the Python
forecast (or a pre-adjustment layer) automatically:
- Recognize "this week has `full_week_impact=TRUE`"
- Compare to the correctly-aligned PY week (where the same holiday was)
- Or exclude that week from the trailing_4wk_yoy signal

---

## 10. Proposed `marketing_holiday_calendar` schema

### 10.1 Design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Grain | One row per (holiday_date, holiday_name) | Matches Calendar col I. Holy Week = 1 row on Palm Sunday (user confirmed). |
| Week rollup | Provided via view, not base table | Keep source-of-truth at daily grain; derive weekly for consumers. |
| Classification | Store as 3 booleans (same_day_of_week, same_date, full_week_impact) | Matches Calendar!AJ:AL exactly. Easy to reason about. |
| Multi-year | Natural: one row per holiday per year | 2022 MLK and 2023 MLK are separate rows with different dates. |
| Test prep releases | Same table, with `event_type` column (`holiday` or `test_release`) | SAT/ACT live in the same AI:AM table today. Co-locating avoids proliferation. |
| Supersede | Soft (is_current + superseded_at/by) | Consistent with marketing_forecast_vintages. Analyst might correct a date. |
| Canada weightings | **Not in this iteration** | User said International/Canada is immaterial; de-scope. |

### 10.2 Proposed DDL

```sql
CREATE TABLE marketing_holiday_calendar (
    id                BIGSERIAL PRIMARY KEY,
    holiday_date      DATE        NOT NULL,
    holiday_name      TEXT        NOT NULL,
    event_type        TEXT        NOT NULL
                                  CHECK (event_type IN ('holiday', 'test_release')),
    same_day_of_week  BOOLEAN     NOT NULL DEFAULT FALSE,
    same_date         BOOLEAN     NOT NULL DEFAULT FALSE,
    full_week_impact  BOOLEAN     NOT NULL DEFAULT FALSE,
    week_start        DATE        NOT NULL,   -- Sunday of the holiday's week
    notes             TEXT,

    -- Audit / soft-supersede (matches marketing_forecast_vintages)
    ingested_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ingested_by       TEXT        NOT NULL DEFAULT 'load_holiday_calendar.py',
    source            TEXT        NOT NULL DEFAULT 'sheet:Calendar',
    is_current        BOOLEAN     NOT NULL DEFAULT TRUE,
    superseded_at     TIMESTAMPTZ,
    superseded_by     BIGINT REFERENCES marketing_holiday_calendar(id),
    supersede_reason  TEXT,

    CHECK ((superseded_at IS NULL) = is_current),
    CHECK (NOT (same_day_of_week AND same_date))  -- mutually exclusive per Calendar logic
);

CREATE UNIQUE INDEX marketing_holiday_calendar_current_uq
    ON marketing_holiday_calendar(holiday_date, holiday_name)
    WHERE is_current;

CREATE INDEX marketing_holiday_calendar_week_idx
    ON marketing_holiday_calendar(week_start)
    WHERE is_current;

-- ── Views ────────────────────────────────────────────────────────────

CREATE VIEW marketing_holiday_calendar_current AS
    SELECT * FROM marketing_holiday_calendar WHERE is_current;

-- Weekly rollup: which week has which event(s), is any full-week-impact?
CREATE VIEW marketing_holiday_weeks_current AS
    SELECT
        week_start,
        STRING_AGG(holiday_name, ', ' ORDER BY holiday_date) AS event_names,
        BOOL_OR(full_week_impact) AS has_full_week_impact,
        COUNT(*) AS event_count
    FROM marketing_holiday_calendar_current
    GROUP BY week_start;

COMMENT ON TABLE marketing_holiday_calendar IS
    'Sourced from Marketing Model "Calendar" tab columns I (Holiday Input) + AI:AM (attribute table). One row per (holiday_date, holiday_name). Full-week-impact events are the ones that distort weekly top-line YoY comparisons and today are corrected manually in Top Line!U (Week Adj).';

COMMENT ON COLUMN marketing_holiday_calendar.same_day_of_week IS
    'Holiday falls on the same weekday every year (MLK=3rd Mon). From Calendar!AJ.';
COMMENT ON COLUMN marketing_holiday_calendar.same_date IS
    'Holiday falls on the same calendar date every year (Jul 4, Dec 25). From Calendar!AK.';
COMMENT ON COLUMN marketing_holiday_calendar.full_week_impact IS
    'The entire week''s top-line leads shift vs a non-holiday week. These are the events that need Week Adj in Top Line. From Calendar!AL.';
```

### 10.3 Expected row counts (2022–2027, 6 years)

| Event | Years | Full Week | Rows |
|---|---|---|---|
| New Year's Day | 6 | no | 6 |
| MLK Day | 6 | no | 6 |
| Presidents' Day | 6 | no | 6 |
| Memorial Day | 6 | no | 6 |
| Juneteenth | 6 | no | 6 |
| Independence Day | 6 | no | 6 |
| Labor Day | 6 | no | 6 |
| Columbus Day | 6 | no | 6 |
| Veterans Day | 6 | no | 6 |
| Thanksgiving | 6 | **yes** | 6 |
| Christmas | 6 | **yes** | 6 |
| Christmas Eve | 6 | no | 6 |
| New Year's Eve | 6 | no | 6 |
| Spring Break 1 | 6 | **yes** | 6 |
| Spring Break 2 | 6 | **yes** | 6 |
| Holy Week | 6 | **yes** | 6 |
| Easter | 6 | no | 6 |
| SAT | ~7/yr × 6 | **yes** | ~42 |
| ACT | ~7/yr × 6 | **yes** | ~42 |
| **Total** | | | **~190** |

Small enough to load in one pass.

### 10.4 Proposed loader: `load_holiday_calendar.py`

```
1. Read Calendar tab range I2:I2200 (Holiday Input) via read_from_sheets --render formula
2. Read Calendar AI:AM attribute lookup
3. Read R/S columns for ACT / SAT release flags
4. For each non-empty col I cell:
   - date = Calendar!B<row>
   - name = Calendar!I<row>
   - attrs = lookup name in AI:AM
   - week_start = Calendar!D<row>
   - event_type = 'test_release' if name in {'SAT', 'ACT'} else 'holiday'
5. Upsert into marketing_holiday_calendar with is_current=TRUE, superseding if
   (holiday_date, holiday_name) exists and changed.
```

---

## 11. Post-holiday-calendar: what comes next

The holiday calendar table is just the data layer. The actual value unlock
requires using it:

| Step | Who | Effort |
|---|---|---|
| 1. Build `marketing_holiday_calendar` table + loader | me | small |
| 2. Backfill 2022–2027 from the sheet | me | small |
| 3. Add a Python adjustment layer that computes `holiday_week_adj` per (BU × week) using historical actuals (reads from `marketing_leads_daily_actuals` once that's populated) | later | medium |
| 4. Have `generate_forecast()` in Supabase consume this adjustment OR expose it as a new column on `Supabase Forecast` that Top Line reads automatically | later | medium |
| 5. Retire manual Week Adj inputs for full-week-impact holidays | when (3)+(4) land | trivial |

**Blocker for step 3:** `marketing_leads_daily_actuals` is currently empty.
Per the Phase 1 handoff, getting it populated (via daily Looker webhook) is
the next major unlock.

---

## 12. Running open questions

| # | Question | Status |
|---|---|---|
| 1 | Is Easter tagged in Calendar!I? | ✅ Yes (AI:AM row 19) |
| 2 | Is Holy Week `full_week_impact=TRUE`? | ✅ Yes (AI:AM row 18: FALSE, FALSE, **TRUE**) |
| 3 | How is Holy Week tagged (1 row on Palm Sunday vs 7 rows Mon–Sun)? | ✅ User confirmed: 1 row on Palm Sunday. Consistent with formula fan-out in cols J–Q. |
| 4 | Does the Top Line tab have manual Easter/Holy Week adjustments? | ✅ Yes, observed: `U56=-0.10`, `U57=-0.03`, `V60=-0.15`. |
| 5 | Does the Python pipeline know about holidays today? | ❌ No — columns suggest trailing_4wk_yoy-based forecasting only. |
| 6 | Should Canada/International get separate weightings? | ✅ User: no, immaterial. De-scoped. |
| 7 | Should test-prep releases (SAT/ACT) share the holiday table? | **Proposal**: yes, via `event_type` column. Co-located in Calendar AI:AM table today. |
| 8 | Will `marketing_leads_daily_actuals` be populated in time to DERIVE holiday weightings from actuals? | ⏳ Not yet. Loader can land independently; weighting derivation is step 3. |

---

## 13. Artifacts / references

- Live model: "Marketing Model - Live" Google Sheet (service account has access)
- Read helper: `read_from_sheets.py` (always use `--render formula` to avoid IMPORTRANGE timeouts)
- Current Supabase tables:
  - `marketing_forecast_vintages` — 175,015 rows, 9 plans backfilled
  - `marketing_forecast_vintages_current` (view)
  - `marketing_leads_daily_actuals` — **empty**, awaiting daily webhook
  - `marketing_leads_daily_actuals_current` (view)
  - `marketing_forecast_vs_actual_daily` (view; empty RHS today)
- Supabase Edge Function: `bright-worker` (Looker ingest + `generate_forecast()` RPC)
- Key Supabase RPC: `generate_forecast()` — runs weekly baseline model (not visible in this repo)
