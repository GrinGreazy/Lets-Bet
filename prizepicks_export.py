"""
prizepicks_export.py

Fetches all PrizePicks prop lines for a given sport and saves them to a
dated folder: ./<MMDDYYYY>-<SPORT>/prizepicks_<SPORT>.csv

Intended to be run first in the pipeline — analyze_props_confidence.py
will auto-detect the output folder from this script.

NOTE: PrizePicks blocks requests from corporate/managed networks.
      Run this script from a personal machine or Google Colab if needed.

Usage
-----
python prizepicks_export.py --sport MLB
python prizepicks_export.py --sport NBA
python prizepicks_export.py  # prompts for sport
"""

from __future__ import annotations

import argparse
import csv
import urllib3
from datetime import datetime
from pathlib import Path

import requests
from requests.exceptions import SSLError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://partner-api.prizepicks.com"

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://app.prizepicks.com/",
    "Origin":          "https://app.prizepicks.com",
}

LEAGUE_IDS = {
    "MLB": 2,
}

CSV_FIELDS = [
    "sport",
    "player",
    "team",
    "position",
    "opponent",
    "stat_type",
    "line",
    "odds_type",
    "start_time",
]


def fetch_projections(league_id: int, verify_ssl: bool = True) -> dict:
    params = {
        "league_id":   league_id,
        "per_page":    1000,
        "single_stat": "true",
    }
    response = requests.get(
        f"{BASE_URL}/projections",
        params=params,
        headers=HEADERS,
        timeout=30,
        verify=verify_ssl,
    )
    response.raise_for_status()
    return response.json()


def extract_rows(payload: dict, sport: str) -> list[dict]:
    included = {
        (item["type"], str(item["id"])): item
        for item in payload.get("included", [])
    }

    rows = []
    for proj in payload.get("data", []):
        attrs  = proj.get("attributes", {})
        rel    = proj.get("relationships", {})

        player_id = str(rel.get("new_player", {}).get("data", {}).get("id", ""))
        player    = included.get(("new_player", player_id), {}).get("attributes", {})

        rows.append({
            "sport":      sport,
            "player":     player.get("name"),
            "team":       player.get("team"),
            "position":   player.get("position"),
            "opponent":   player.get("opponent"),
            "stat_type":  attrs.get("stat_type"),
            "line":       attrs.get("line_score"),
            "odds_type":  attrs.get("odds_type"),
            "start_time": attrs.get("start_time"),
        })
    return rows


def save_csv(rows: list[dict], sport: str, output_root: Path) -> Path:
    date_str = datetime.now().strftime("%m%d%Y")
    folder   = output_root / f"{date_str}-{sport.upper()}"
    folder.mkdir(parents=True, exist_ok=True)

    out = folder / f"prizepicks_{sport.upper()}.csv"

    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export PrizePicks prop lines to a dated CSV folder.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Sport is fixed to MLB
    # parser.add_argument("--sport", ...)  # reserved for future use
    parser.add_argument(
        "--output-root",
        default=".",
        help="Directory where the MMDDYYYY-SPORT folder will be created (default: current dir).",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable SSL certificate verification (use on managed/corporate networks).",
    )
    args, _ = parser.parse_known_args()
    return args


def main() -> None:
    args = parse_args()

    sport = "MLB"

    if sport not in LEAGUE_IDS:
        print(f"Unknown sport '{sport}'. Choose from: {', '.join(LEAGUE_IDS)}")
        return

    print(f"Fetching {sport} props from PrizePicks...")

    try:
        payload = fetch_projections(LEAGUE_IDS[sport], verify_ssl=not args.insecure)
    except SSLError:
        if args.insecure:
            raise
        print("SSL validation failed — retrying without SSL verification...")
        payload = fetch_projections(LEAGUE_IDS[sport], verify_ssl=False)

    rows = extract_rows(payload, sport)

    if not rows:
        print("No props found for this sport.")
        return

    path = save_csv(rows, sport, Path(args.output_root))
    print(f"Saved {len(rows)} props → {path}")
    print(f"\nNext step: python analyze_props_confidence.py")


if __name__ == "__main__":
    main()
