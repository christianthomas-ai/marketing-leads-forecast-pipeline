"""Create a local (gitignored) .env for running push_to_sheets.py.

Reads Business Planning credentials from a sibling Project WillyBot/.env
and points GOOGLE_CREDS_FILE at that repo's service-account JSON.

WillyBot often stores a publishable key. generate_forecast() needs the
legacy service_role JWT, so if SUPABASE_ACCESS_TOKEN is set this script
fetches that key from the Management API. Does not print secret values.
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
WILLYBOT = HERE.parent / "Project WillyBot"
PROJECT_REF = "tuhuajzagxuvkxxowsxo"


def _parse_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _looks_like_jwt(token: str) -> bool:
    return token.startswith("eyJ") and token.count(".") == 2


def _fetch_service_role_jwt() -> str:
    token = os.environ.get("SUPABASE_ACCESS_TOKEN")
    if not token:
        raise SystemExit(
            "WillyBot key is not a service_role JWT. Set SUPABASE_ACCESS_TOKEN "
            "and re-run so this script can fetch service_role."
        )
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{PROJECT_REF}/api-keys",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(req) as resp:
        keys = json.loads(resp.read().decode())
    row = next((k for k in keys if k.get("name") == "service_role"), None)
    secret = (row or {}).get("api_key") or (row or {}).get("key") or ""
    if not _looks_like_jwt(secret):
        raise SystemExit("Management API did not return a service_role JWT.")
    return secret


def main() -> None:
    env_path = WILLYBOT / ".env"
    if not env_path.exists():
        raise SystemExit(f"Missing {env_path}")
    src = _parse_env(env_path)
    url = src.get("SUPABASE_BUSINESS_PLANNING_URL") or src.get("SUPABASE_FINANCE_URL")
    key = src.get("SUPABASE_BUSINESS_PLANNING_KEY") or src.get("SUPABASE_FINANCE_KEY")
    creds = WILLYBOT / "credentials.json"
    if not url or not key:
        raise SystemExit("Missing SUPABASE_BUSINESS_PLANNING_URL/KEY in WillyBot .env")
    if not creds.exists():
        raise SystemExit(f"Missing {creds}")
    if not _looks_like_jwt(key):
        key = _fetch_service_role_jwt()

    dest = HERE / ".env"
    dest.write_text(
        "\n".join(
            [
                f"SUPABASE_URL={url}",
                f"SUPABASE_SERVICE_ROLE_KEY={key}",
                f"GOOGLE_CREDS_FILE={creds}",
                "SPREADSHEET_NAME=Marketing Model - Live",
                "WORKSHEET_NAME=Supabase Forecast",
                "MAX_ACTUALS_AGE_DAYS=3",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(f"supabase_host={url.split('//', 1)[-1].split('/', 1)[0]}")
    print(f"google_creds={creds.name} exists={creds.exists()}")
    print(f"wrote {dest.name} (gitignored)")


if __name__ == "__main__":
    main()
