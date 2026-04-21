"""Verify invariants of marketing_forecast_vintages.

One-shot diagnostics script for the 2-4 week shadow period and for
post-rename / post-backfill sanity checking. Prints:

  1. Row count totals (all rows vs is_current=TRUE rows).
  2. Plan inventory: one row per (plan_type, plan_start_date) with counts
     broken down by is_current (TRUE/FALSE).
  3. Invariant checks:
     a. No row with is_current=TRUE AND superseded_at IS NOT NULL.
     b. No row with is_current=FALSE AND superseded_at IS NULL.
        (Every not-current row must record when it was superseded.)
     c. Every superseded_by FK points at an existing row.

Non-zero exit if any invariant fails so this script is CI-friendly.

Why not plain SQL? PostgREST (Supabase's HTTP API) doesn't expose raw SQL
without an RPC, and adding an RPC just for a diagnostics script feels like
overkill. Fetching the 175k-row key projection (plan_type, plan_start_date,
is_current, superseded_at, id, superseded_by) is ~10 MB — well under any
limit and finishes in a few seconds.
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
TABLE = "marketing_forecast_vintages"
# PostgREST caps a single response at 1000 rows no matter what. Paginate
# with limit/offset (same pattern as memorialize_forecast.fetch_current_vintage
# and reconcile_vintage.fetch_supabase_vintage).
PAGE_SIZE = 1000


def _die(msg: str, code: int = 2) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _validate_env() -> None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        _die("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in env/.env")


def fetch_all(select: str) -> list[dict[str, Any]]:
    """Paginate over the base table fetching only the requested columns."""
    out: list[dict[str, Any]] = []
    offset = 0
    with httpx.Client(timeout=60) as client:
        while True:
            resp = client.get(
                f"{SUPABASE_URL}/rest/v1/{TABLE}",
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                },
                params={
                    "select": select,
                    "order": "id.asc",
                    "limit": PAGE_SIZE,
                    "offset": offset,
                },
            )
            if resp.status_code != 200:
                _die(
                    f"Failed to fetch {TABLE} (HTTP {resp.status_code}): {resp.text[:400]}"
                )
            batch = resp.json()
            if not batch:
                break
            out.extend(batch)
            if len(batch) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
            if offset % 50000 == 0:
                print(f"  ...fetched {offset:,} rows")
    return out


def main() -> int:
    _validate_env()

    print(f"Fetching key projection from {TABLE}...")
    rows = fetch_all(
        "id,plan_type,plan_start_date,is_current,superseded_at,superseded_by"
    )
    print(f"  Got {len(rows):,} rows.\n")

    # ── Row count totals ───────────────────────────────────────────────
    total = len(rows)
    current = sum(1 for r in rows if r["is_current"])
    superseded = total - current
    print(f"Totals:  all={total:,}  is_current=TRUE={current:,}  is_current=FALSE={superseded:,}\n")

    # ── Plan inventory ─────────────────────────────────────────────────
    by_plan: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"current": 0, "superseded": 0}
    )
    for r in rows:
        key = (r["plan_type"], r["plan_start_date"])
        bucket = "current" if r["is_current"] else "superseded"
        by_plan[key][bucket] += 1

    print(f"Plan inventory ({len(by_plan)} distinct plans):")
    print(f"  {'plan_type':<6}  {'plan_start_date':<15}  {'current':>8}  {'superseded':>11}")
    for (pt, psd), counts in sorted(by_plan.items()):
        print(
            f"  {pt:<6}  {psd:<15}  {counts['current']:>8,}  {counts['superseded']:>11,}"
        )
    print()

    # ── Invariant checks ───────────────────────────────────────────────
    failures: list[str] = []

    # (a) is_current=TRUE AND superseded_at IS NOT NULL → contradictory
    bad_a = [r for r in rows if r["is_current"] and r["superseded_at"] is not None]
    if bad_a:
        failures.append(
            f"(a) {len(bad_a)} rows have is_current=TRUE AND superseded_at NOT NULL "
            f"(first id: {bad_a[0]['id']})"
        )

    # (b) is_current=FALSE AND superseded_at IS NULL → incomplete supersede
    bad_b = [r for r in rows if not r["is_current"] and r["superseded_at"] is None]
    if bad_b:
        failures.append(
            f"(b) {len(bad_b)} rows have is_current=FALSE AND superseded_at NULL "
            f"(first id: {bad_b[0]['id']})"
        )

    # (c) superseded_by must FK to an existing row (Postgres enforces this
    # structurally, but we verify the join resolves within what we fetched).
    ids = {r["id"] for r in rows}
    bad_c = [
        r
        for r in rows
        if r["superseded_by"] is not None and r["superseded_by"] not in ids
    ]
    if bad_c:
        failures.append(
            f"(c) {len(bad_c)} rows have superseded_by pointing at a missing id"
        )

    # (d) Each plan should have AT LEAST one is_current row. A plan with
    # zero current rows means we dropped the current vintage without
    # writing a replacement.
    bad_d = [k for k, v in by_plan.items() if v["current"] == 0]
    if bad_d:
        failures.append(
            f"(d) {len(bad_d)} plans have zero is_current rows: "
            f"{bad_d[:5]}{' ...' if len(bad_d) > 5 else ''}"
        )

    # Report
    if failures:
        print("INVARIANT FAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("All invariants OK:")
    print("  (a) no row is both is_current=TRUE and superseded_at NOT NULL")
    print("  (b) every superseded row has a non-NULL superseded_at")
    print("  (c) every superseded_by points at an existing row")
    print("  (d) every plan has at least one is_current=TRUE row")
    return 0


if __name__ == "__main__":
    sys.exit(main())
