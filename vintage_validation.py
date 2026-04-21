"""Shared validation + parsing helpers for marketing_forecast_vintages read/write paths.

Used by memorialize_forecast.py (write path) and reconcile_vintage.py (audit
path) so that the two sides agree on what constitutes a "valid" row.

Validation layers
-----------------
1. **Structural** (hard-fail): required columns exist, dates parse, grain
   columns non-empty. Anything that would corrupt the table if written.
2. **Range** (hard-fail): metric values are non-negative or NULL.
3. **Row count sanity** (hard-fail): an out-of-band row count almost always
   means the Sheet tab is the wrong shape (empty tab, duplicated paste,
   dev test left behind). Bounds are conservative and overridable via env.
4. **Known-value** (warning-only by default): business / lead_source /
   audience match the values documented in the migration. Warnings go to
   stderr but do NOT abort — the Sheet remains source of truth and new
   values are expected over time (see migration comments). Pass
   ``strict=True`` to ``validate_rows`` to escalate these to hard failures.

Expected values come from the migration file's inline comments. Keep these
in sync with supabase/migrations/2026-04-20_forecast_vintages.sql (and the
rename migration 2026-04-21_rename_marketing_tables.sql).
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

# ── KNOWN VALUES (warning-only by default) ───────────────────────────
# Source: inline comments in the marketing_forecast_vintages migration.
KNOWN_BUSINESSES: frozenset[str] = frozenset({"VT Core", "International", "Prof Certs"})

KNOWN_LEAD_SOURCES: frozenset[str] = frozenset(
    {
        "Brand & Direct",
        "Meta - Phone",
        "Other",
        "Phone",
        "Search - Bing",
        "Search - Non Tutor",
        "Search - Tutor PPC",
        "Search - Tutor SEO",
    }
)

KNOWN_AUDIENCES: frozenset[str] = frozenset(
    {
        "Col-STEM",
        "Grad Test Prep",
        "HS-STEM",
        "International",
        "International-CAN",
        "K-6",
        "K12 Test Prep",
        "Languages",
        "Learning Differences",
        "Other",
        "Prof Certs",
        "Unknown",
        "Upskilling",
    }
)

# Header aliases: sheet-native column name -> canonical column name. Applied
# after normalize_header() (lowercase + snake_case). The "Daily Forecast -
# LOCKED For Summary Tables" tab uses "Date" and "Channel" where the scripts
# and the Supabase schema expect "forecast_date" and "lead_source". If the
# Sheet is ever renamed to match, the aliases become no-ops.
HEADER_ALIASES: dict[str, str] = {
    "date": "forecast_date",
    "channel": "lead_source",
}

# Google Sheets / Excel serial date epoch (day 0 = 1899-12-30).
SHEET_DATE_EPOCH = date(1899, 12, 30)


# Row count sanity bounds. Weekly SOP has been ~2-3k rows historically (daily
# grain x ~3 BUs x ~6 lead sources x ~12 audiences, with many combos empty).
# Quarterly plans may be larger. These bounds just catch "the Sheet is empty"
# and "something copy-pasted 50 plans worth of data".
MIN_ROWS = int(os.environ.get("VINTAGE_MIN_ROWS", "50"))
MAX_ROWS = int(os.environ.get("VINTAGE_MAX_ROWS", "100000"))


# ── PARSING ──────────────────────────────────────────────────────────

def parse_date_str(s: str, field_name: str) -> str:
    """Validate that ``s`` is YYYY-MM-DD and return it unchanged."""
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"{field_name}: expected YYYY-MM-DD, got '{s}'")
    return s


def parse_float_maybe(raw: Any) -> Optional[float]:
    """Parse a float, tolerating None, empty, '$', commas, 'N/A'."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    s = str(raw).strip()
    if s == "" or s.lower() in {"n/a", "na", "null", "#n/a"}:
        return None
    s = s.replace(",", "").replace("$", "")
    try:
        return float(s)
    except ValueError:
        raise ValueError(f"Could not parse numeric value: '{raw}'")


# ── HEADER NORMALIZATION ─────────────────────────────────────────────

def normalize_header(cells: Iterable[str]) -> list[str]:
    """Lower-case, strip, and snake_case a header row. Stable & whitespace-tolerant."""
    return [c.strip().lower().replace(" ", "_") for c in cells]


def apply_aliases(header: list[str]) -> list[str]:
    """Remap sheet-native header names to the canonical ones used downstream."""
    return [HEADER_ALIASES.get(h, h) for h in header]


def find_header_row(
    grid: list[list[str]], required: set[str], max_scan: int = 5
) -> int:
    """Return the index of the first row (among the first ``max_scan``) whose
    normalized + aliased header contains every column in ``required``.

    The locked forecast tab's row 1 is a SUMPRODUCT metadata row; the real
    header sits on row 2. Rather than hard-code that offset (fragile if the
    tab ever gets reorganized), we scan for the first row that looks like
    a real header.
    """
    for i, row in enumerate(grid[:max_scan]):
        header = apply_aliases(normalize_header(row))
        if required.issubset(set(header)):
            return i
    first_few = [apply_aliases(normalize_header(r)) for r in grid[:max_scan]]
    raise ValidationError(
        f"Could not find a header row in the first {max_scan} rows. "
        f"Required columns (post-alias): {sorted(required)}. "
        f"Rows scanned (normalized + aliased): {first_few}"
    )


# ── DATE PARSING (tolerates both ISO strings and Sheet serial numbers) ─

def parse_sheet_date(raw: Any) -> str:
    """Normalize a Sheet date cell to ISO ``YYYY-MM-DD``.

    Accepts:
      - ISO strings ("2026-04-26")
      - Integer or float serial dates ("46089", 46089.0)
      - Strings that happen to be numeric serials ("46089")

    Raises ``ValueError`` on anything else. Callers should catch + map to
    a row-level error message.
    """
    if raw is None:
        raise ValueError("date cell is empty")
    s = str(raw).strip()
    if s == "":
        raise ValueError("date cell is empty")
    # Already ISO?
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return parse_date_str(s, "forecast_date")
    # Else: try to interpret as a Sheets/Excel serial number.
    try:
        serial = float(s)
    except ValueError:
        raise ValueError(f"Unparseable date value: {raw!r}")
    # Serial date sanity bounds: 1 = 1899-12-31, 200000 ≈ 2447. Anything
    # outside this is almost certainly not meant to be a date.
    if not (1 <= serial < 200000):
        raise ValueError(f"Date serial out of plausible range: {serial}")
    return (SHEET_DATE_EPOCH + timedelta(days=int(serial))).isoformat()


# ── VALIDATION ERROR TYPE ────────────────────────────────────────────

class ValidationError(Exception):
    """Raised when a vintage row set fails a hard-fail validation rule."""


# ── ROW VALIDATION ───────────────────────────────────────────────────

def validate_rows(
    rows: list[dict[str, Any]],
    *,
    strict: bool = False,
    min_rows: int = MIN_ROWS,
    max_rows: int = MAX_ROWS,
    allow_negative_metrics: bool = False,
) -> None:
    """Validate a parsed vintage row set. Raises ``ValidationError`` on hard fail.

    Expected row shape (dict with at least these keys):
        forecast_date: str (YYYY-MM-DD)
        business: str
        lead_source: str
        audience: str
        leads: float | None
        ad_spend: float | None

    ``strict=True`` turns the known-value warnings into hard failures.

    ``allow_negative_metrics=True`` downgrades negative ``leads`` / ``ad_spend``
    from hard-fail to a stderr warning. Use for historical backfill, where
    negatives are real correction/reversal rows we can't edit out. Going-
    forward weekly locks should leave this False so new data bugs surface.
    """
    # 1. Row count sanity
    n = len(rows)
    if n < min_rows:
        raise ValidationError(
            f"Too few rows: got {n}, expected at least {min_rows}. "
            f"Is the Sheet tab empty or was a filter applied before reading? "
            f"Override with VINTAGE_MIN_ROWS env var."
        )
    if n > max_rows:
        raise ValidationError(
            f"Too many rows: got {n}, expected at most {max_rows}. "
            f"Did multiple plans' worth of data get pasted into the tab? "
            f"Override with VINTAGE_MAX_ROWS env var."
        )

    # 2. Structural + range checks
    negative_count = 0
    for i, r in enumerate(rows, start=2):  # row 1 is header in the Sheet
        parse_date_str(r["forecast_date"], f"row {i} forecast_date")
        for k in ("business", "lead_source", "audience"):
            v = r.get(k, "")
            if not isinstance(v, str) or not v.strip():
                raise ValidationError(
                    f"Row {i}: '{k}' must be non-empty string, got {v!r}"
                )
        for k in ("leads", "ad_spend"):
            v = r.get(k)
            if v is not None and v < 0:
                if allow_negative_metrics:
                    negative_count += 1
                else:
                    raise ValidationError(
                        f"Row {i}: '{k}' must be >= 0 (or NULL), got {v}. "
                        f"Pass allow_negative_metrics=True to treat this as a "
                        f"warning (intended for historical backfill only)."
                    )

    if negative_count:
        print(
            f"WARNING: {negative_count} row(s) had negative leads or ad_spend. "
            f"Accepted because allow_negative_metrics=True (treat as correction/"
            f"reversal entries from source data).",
            file=sys.stderr,
        )

    # 3. Known-value check (warning-only, or hard-fail if strict)
    unknown_bu = {r["business"] for r in rows} - KNOWN_BUSINESSES
    unknown_ls = {r["lead_source"] for r in rows} - KNOWN_LEAD_SOURCES
    unknown_aud = {r["audience"] for r in rows} - KNOWN_AUDIENCES

    unknowns: list[tuple[str, set[str]]] = []
    if unknown_bu:
        unknowns.append(("business", unknown_bu))
    if unknown_ls:
        unknowns.append(("lead_source", unknown_ls))
    if unknown_aud:
        unknowns.append(("audience", unknown_aud))

    if unknowns:
        lines = [f"Unknown dimension values encountered (not in migration comments):"]
        for dim, vals in unknowns:
            lines.append(f"  {dim}: {sorted(vals)}")
        msg = "\n".join(lines)
        if strict:
            raise ValidationError(
                msg + "\n(strict mode; update vintage_validation.KNOWN_* to accept new values)"
            )
        print(f"WARNING: {msg}", file=sys.stderr)
        print(
            "WARNING: Proceeding — the Sheet is the source of truth per migration "
            "design. Add these values to vintage_validation.KNOWN_* when they become "
            "recurring.",
            file=sys.stderr,
        )
