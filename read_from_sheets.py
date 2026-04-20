"""Read-only reader for the live Marketing Model Google Sheet.

Designed for two uses:

1. CLI — on-demand inspection from a terminal (including the Cursor agent):
     python read_from_sheets.py --list
     python read_from_sheets.py "Supabase Forecast"
     python read_from_sheets.py "Top Line" --range A1:AU200
     python read_from_sheets.py "Top Line" --format json
     python read_from_sheets.py "Top Line" --head 20
     python read_from_sheets.py "Top Line" --render formatted --range A1:D5

2. Module — importable by future scripts (e.g. the forecast_vintages write
   path, reconciliation checks, validation) without re-implementing auth:
     from read_from_sheets import read_worksheet, list_worksheets
     rows = read_worksheet("Top Line")                      # formulas
     rows = read_worksheet("Top Line", render="formatted")  # computed values

Auth reuses the existing service account credentials.json (same as
push_to_sheets.py), but requests *read-only* scopes so this module cannot
accidentally mutate the sheet even if a caller tries.

Why the default render is FORMULA, not the computed value:
    The live model's browser-side recalc is ~1-4 min per top-line change
    (heavy formula graph, IMPORTRANGE for actuals). The Sheets API's
    default render option (FORMATTED_VALUE) forces the backend to re-run
    that same recalc before returning anything, which blows past Google's
    ~2-3 min API timeout and comes back as HTTP 503. The FORMULA render
    option returns raw formula strings without evaluating, so it sidesteps
    the recalc and returns in ~100 ms. For computed values, prefer reading
    directly from Supabase (source of truth), or pass --render formatted
    on a small pre-warmed range. See PROJECT_MANIFEST.md.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from typing import Optional

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

GOOGLE_CREDS_FILE = os.environ.get("GOOGLE_CREDS_FILE", "credentials.json")
DEFAULT_SPREADSHEET_NAME = os.environ.get("SPREADSHEET_NAME", "Marketing Model - Live")

READONLY_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

# Map friendly names to the Google Sheets API valueRenderOption values.
# See https://developers.google.com/sheets/api/reference/rest/v4/ValueRenderOption
_RENDER_OPTIONS = {
    "formula": "FORMULA",              # raw formula strings; no recalc triggered
    "formatted": "FORMATTED_VALUE",    # what the user sees; triggers recalc on stale cells
    "unformatted": "UNFORMATTED_VALUE",# numbers/dates without display formatting
}
DEFAULT_RENDER = "formula"


def get_client() -> gspread.Client:
    """Return an authorized gspread client using read-only scopes."""
    if not os.path.exists(GOOGLE_CREDS_FILE):
        print(
            f"ERROR: credentials file not found at '{GOOGLE_CREDS_FILE}'.\n"
            "  Set GOOGLE_CREDS_FILE env var or place credentials.json in the "
            "current directory.",
            file=sys.stderr,
        )
        sys.exit(2)

    creds = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=READONLY_SCOPES)
    return gspread.authorize(creds)


def _open_spreadsheet(name: Optional[str] = None) -> gspread.Spreadsheet:
    client = get_client()
    try:
        return client.open(name or DEFAULT_SPREADSHEET_NAME)
    except gspread.exceptions.SpreadsheetNotFound:
        print(
            f"ERROR: spreadsheet '{name or DEFAULT_SPREADSHEET_NAME}' not found or not "
            "shared with the service account.",
            file=sys.stderr,
        )
        sys.exit(3)


def list_worksheets(spreadsheet_name: Optional[str] = None) -> list[dict]:
    """Return metadata for every worksheet in the spreadsheet.

    Each entry includes ``title``, ``row_count``, ``col_count``, and ``id``.
    """
    sh = _open_spreadsheet(spreadsheet_name)
    return [
        {
            "title": ws.title,
            "row_count": ws.row_count,
            "col_count": ws.col_count,
            "id": ws.id,
        }
        for ws in sh.worksheets()
    ]


def _resolve_render(render: str) -> str:
    try:
        return _RENDER_OPTIONS[render]
    except KeyError as e:
        raise ValueError(
            f"Unknown render option '{render}'. Expected one of: "
            f"{', '.join(_RENDER_OPTIONS)}"
        ) from e


def read_worksheet(
    worksheet_name: str,
    spreadsheet_name: Optional[str] = None,
    render: str = DEFAULT_RENDER,
) -> list[list[str]]:
    """Return every cell in ``worksheet_name`` as a list-of-lists of strings.

    Row 0 is the first row of the sheet (which may or may not be a header —
    caller decides). Empty trailing columns are trimmed by gspread.

    ``render`` controls how cells are returned:
      - 'formula'     (default) raw formula strings, no recalc triggered
      - 'formatted'   what the user sees; may trigger recalc on stale cells
      - 'unformatted' numbers/dates as raw values without display formatting
    """
    sh = _open_spreadsheet(spreadsheet_name)
    try:
        ws = sh.worksheet(worksheet_name)
    except gspread.exceptions.WorksheetNotFound:
        titles = [w.title for w in sh.worksheets()]
        print(
            f"ERROR: worksheet '{worksheet_name}' not found. Available:\n  "
            + "\n  ".join(titles),
            file=sys.stderr,
        )
        sys.exit(4)
    return ws.get_all_values(value_render_option=_resolve_render(render))


def read_range(
    worksheet_name: str,
    a1_range: str,
    spreadsheet_name: Optional[str] = None,
    render: str = DEFAULT_RENDER,
) -> list[list[str]]:
    """Return a specific A1-style range (e.g. 'A1:Z100').

    See :func:`read_worksheet` for the ``render`` options.
    """
    sh = _open_spreadsheet(spreadsheet_name)
    ws = sh.worksheet(worksheet_name)
    return ws.get(a1_range, value_render_option=_resolve_render(render))


def _format_rows(rows: list[list[str]], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(rows, ensure_ascii=False, indent=2)
    if fmt in ("csv", "tsv"):
        delimiter = "," if fmt == "csv" else "\t"
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
        writer.writerows(rows)
        return buf.getvalue()
    raise ValueError(f"Unknown format: {fmt}")


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Read-only reader for the live Marketing Model Google Sheet.",
    )
    p.add_argument(
        "worksheet",
        nargs="?",
        help="Worksheet (tab) name to read. Omit and pass --list to see available tabs.",
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="List all worksheets in the spreadsheet and exit.",
    )
    p.add_argument(
        "--range",
        dest="a1_range",
        metavar="A1:Z100",
        help="Read only the given A1-style range from the worksheet.",
    )
    p.add_argument(
        "--format",
        choices=["csv", "tsv", "json"],
        default="csv",
        help="Output format (default: csv).",
    )
    p.add_argument(
        "--head",
        type=int,
        metavar="N",
        help="Only output the first N rows (applied after range, before format).",
    )
    p.add_argument(
        "--spreadsheet",
        dest="spreadsheet_name",
        help=f"Spreadsheet name (default: '{DEFAULT_SPREADSHEET_NAME}', "
        "also overridable via SPREADSHEET_NAME env var).",
    )
    p.add_argument(
        "--render",
        choices=list(_RENDER_OPTIONS),
        default=DEFAULT_RENDER,
        help=(
            "How cells are returned (default: 'formula'). "
            "'formula' returns raw formulas and avoids the recalc timeout that "
            "the default FORMATTED_VALUE hits on the live model. Use 'formatted' "
            "for small pre-warmed ranges only, or read computed values from "
            "Supabase instead."
        ),
    )
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    if args.list:
        sheets = list_worksheets(args.spreadsheet_name)
        print(f"Spreadsheet: {args.spreadsheet_name or DEFAULT_SPREADSHEET_NAME}")
        print(f"Worksheets ({len(sheets)}):")
        for s in sheets:
            print(f"  - {s['title']!r} ({s['row_count']} rows x {s['col_count']} cols)")
        return 0

    if not args.worksheet:
        print(
            "ERROR: must provide a worksheet name, or pass --list.\n"
            "       python read_from_sheets.py --list",
            file=sys.stderr,
        )
        return 1

    if args.a1_range:
        rows = read_range(
            args.worksheet, args.a1_range, args.spreadsheet_name, render=args.render
        )
    else:
        rows = read_worksheet(
            args.worksheet, args.spreadsheet_name, render=args.render
        )

    if args.head is not None:
        rows = rows[: args.head]

    sys.stdout.write(_format_rows(rows, args.format))
    if args.format != "json" and rows and not _format_rows(rows, args.format).endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
