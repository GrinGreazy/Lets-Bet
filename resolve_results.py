"""
resolve_results.py

Scans all dated folders for unresolved predictions and fetches actual
game outcomes from ESPN (NBA) or MLB Stats API (MLB).

A folder is eligible for resolution if:
  - It contains confidence_results.json  (predictions exist)
  - It does NOT contain actual_results.json  (not yet resolved)
  - The earliest predicted game started more than 24 hours ago

Writes actual_results.json to each resolved folder.

Usage
-----
python resolve_results.py                  # auto-scan all unresolved folders
python resolve_results.py --folder ./04282026-MLB  # single folder override
python resolve_results.py --hours 12       # custom wait threshold (default 24)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.exceptions import SSLError
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MLB_PEOPLE_BASE    = "https://statsapi.mlb.com/api/v1/people"


# ============================================================
#  SHARED UTILITIES
# ============================================================

def parse_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def fetch_json(url: str, params: Optional[Dict] = None, insecure: bool = False) -> Dict:
    try:
        r = requests.get(url, params=params, timeout=30, verify=not insecure)
        r.raise_for_status()
        return r.json()
    except SSLError:
        r = requests.get(url, params=params, timeout=30, verify=False)
        r.raise_for_status()
        return r.json()


def normalize_name(name: str) -> str:
    replacements = {
        "\u00e1":"a","\u00e0":"a","\u00e2":"a","\u00e4":"a","\u00e3":"a",
        "\u00e9":"e","\u00e8":"e","\u00ea":"e","\u00eb":"e",
        "\u00ed":"i","\u00ec":"i","\u00ee":"i","\u00ef":"i",
        "\u00f3":"o","\u00f2":"o","\u00f4":"o","\u00f6":"o","\u00f5":"o",
        "\u00fa":"u","\u00f9":"u","\u00fb":"u","\u00fc":"u",
        "\u00f1":"n","\u00e7":"c","\u010d":"c","\u0107":"c",
        "\u017e":"z","\u0161":"s","\u00fd":"y",
    }
    name = name.lower()
    for src, dst in replacements.items():
        name = name.replace(src, dst)
    return re.sub(r"[^a-z0-9]", "", name)


def _parse_start_time(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        normalized = re.sub(r"([+-])(\d{2}):(\d{2})$", r"\1\2\3", raw.rstrip("Z"))
        if raw.endswith("Z"):
            normalized += "+0000"
        return datetime.strptime(normalized, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None


def _outs_from_innings(ip_str: Any) -> float:
    try:
        ip = float(ip_str)
        whole = int(ip)
        remainder = round((ip - whole) * 10)
        return whole * 3 + remainder
    except (TypeError, ValueError):
        return 0.0


# ============================================================
#  STAT EXTRACTORS  (match analyze_props_confidence.py exactly)
# ============================================================

MLB_HITTER_EXTRACTORS = {
    "Hits":                 lambda s: parse_float(s.get("hits",         0)),
    "Runs":                 lambda s: parse_float(s.get("runs",         0)),
    "RBIs":                 lambda s: parse_float(s.get("rbi",          0)),
    "Home Runs":            lambda s: parse_float(s.get("homeRuns",     0)),
    "Stolen Bases":         lambda s: parse_float(s.get("stolenBases",  0)),
    "Total Bases":          lambda s: parse_float(s.get("totalBases",   0)),
    "Doubles":              lambda s: parse_float(s.get("doubles",      0)),
    "Triples":              lambda s: parse_float(s.get("triples",      0)),
    "Walks":                lambda s: parse_float(s.get("baseOnBalls",  0)),
    "Hitter Strikeouts":    lambda s: parse_float(s.get("strikeOuts",   0)),
    "Singles":              lambda s: (
        parse_float(s.get("hits",0)) - parse_float(s.get("doubles",0))
        - parse_float(s.get("triples",0)) - parse_float(s.get("homeRuns",0))
    ),
    "Hits+Runs+RBIs":       lambda s: (
        parse_float(s.get("hits",0)) + parse_float(s.get("runs",0)) + parse_float(s.get("rbi",0))
    ),
    "Hitter Fantasy Score": lambda s: (
        parse_float(s.get("hits",0))*3 + parse_float(s.get("runs",0))*2
        + parse_float(s.get("rbi",0))*2 + parse_float(s.get("baseOnBalls",0))*2
        + parse_float(s.get("stolenBases",0))*3 + parse_float(s.get("homeRuns",0))*3
        - parse_float(s.get("strikeOuts",0))
    ),
}

MLB_PITCHER_EXTRACTORS = {
    "Pitcher Strikeouts":    lambda s: parse_float(s.get("strikeOuts",      0)),
    "Hits Allowed":          lambda s: parse_float(s.get("hits",            0)),
    "Earned Runs Allowed":   lambda s: parse_float(s.get("earnedRuns",      0)),
    "Walks Allowed":         lambda s: parse_float(s.get("baseOnBalls",     0)),
    "Pitches Thrown":        lambda s: parse_float(s.get("numberOfPitches", 0)),
    "Home Runs Allowed":     lambda s: parse_float(s.get("homeRuns",        0)),
    "Pitching Outs":         lambda s: _outs_from_innings(s.get("inningsPitched", 0)),
    "Pitcher Fantasy Score": lambda s: (
        parse_float(s.get("strikeOuts",0))*3 - parse_float(s.get("hits",0))*0.6
        - parse_float(s.get("earnedRuns",0))*3 - parse_float(s.get("baseOnBalls",0))*0.6
        + _outs_from_innings(s.get("inningsPitched",0))*0.5
    ),
}

MLB_HITTER_EXTRACTORS = {
    "Hits":                 lambda s: parse_float(s.get("hits",         0)),
    "Runs":                 lambda s: parse_float(s.get("runs",         0)),
    "RBIs":                 lambda s: parse_float(s.get("rbi",          0)),
    "Home Runs":            lambda s: parse_float(s.get("homeRuns",     0)),
    "Stolen Bases":         lambda s: parse_float(s.get("stolenBases",  0)),
    "Total Bases":          lambda s: parse_float(s.get("totalBases",   0)),
    "Doubles":              lambda s: parse_float(s.get("doubles",      0)),
    "Triples":              lambda s: parse_float(s.get("triples",      0)),
    "Walks":                lambda s: parse_float(s.get("baseOnBalls",  0)),
    "Hitter Strikeouts":    lambda s: parse_float(s.get("strikeOuts",   0)),
    "Singles":              lambda s: (
        parse_float(s.get("hits",0)) - parse_float(s.get("doubles",0))
        - parse_float(s.get("triples",0)) - parse_float(s.get("homeRuns",0))
    ),
    "Hits+Runs+RBIs":       lambda s: (
        parse_float(s.get("hits",0)) + parse_float(s.get("runs",0)) + parse_float(s.get("rbi",0))
    ),
    "Hitter Fantasy Score": lambda s: (
        parse_float(s.get("hits",0))*3 + parse_float(s.get("runs",0))*2
        + parse_float(s.get("rbi",0))*2 + parse_float(s.get("baseOnBalls",0))*2
        + parse_float(s.get("stolenBases",0))*3 + parse_float(s.get("homeRuns",0))*3
        - parse_float(s.get("strikeOuts",0))
    ),
}

MLB_PITCHER_EXTRACTORS = {
    "Pitcher Strikeouts":    lambda s: parse_float(s.get("strikeOuts",      0)),
    "Hits Allowed":          lambda s: parse_float(s.get("hits",            0)),
    "Earned Runs Allowed":   lambda s: parse_float(s.get("earnedRuns",      0)),
    "Walks Allowed":         lambda s: parse_float(s.get("baseOnBalls",     0)),
    "Pitches Thrown":        lambda s: parse_float(s.get("numberOfPitches", 0)),
    "Home Runs Allowed":     lambda s: parse_float(s.get("homeRuns",        0)),
    "Pitching Outs":         lambda s: _outs_from_innings(s.get("inningsPitched", 0)),
    "Pitcher Fantasy Score": lambda s: (
        parse_float(s.get("strikeOuts",0))*3 - parse_float(s.get("hits",0))*0.6
        - parse_float(s.get("earnedRuns",0))*3 - parse_float(s.get("baseOnBalls",0))*0.6
        + _outs_from_innings(s.get("inningsPitched",0))*0.5
    ),
}

NBA_EXTRACTORS = {
    "Points":        lambda g: parse_float(g.get("points",             0)),
    "Rebounds":      lambda g: parse_float(g.get("totalRebounds",      0)),
    "Assists":       lambda g: parse_float(g.get("assists",            0)),
    "Steals":        lambda g: parse_float(g.get("steals",             0)),
    "Blocks":        lambda g: parse_float(g.get("blocks",             0)),
    "Turnovers":     lambda g: parse_float(g.get("turnovers",          0)),
    "3-PT Made":     lambda g: parse_float(g.get("threePointFieldGoalsMade", 0)),
    "Pts+Reb+Ast":   lambda g: (
        parse_float(g.get("points",0)) + parse_float(g.get("totalRebounds",0))
        + parse_float(g.get("assists",0))
    ),
    "Pts+Ast":       lambda g: parse_float(g.get("points",0)) + parse_float(g.get("assists",0)),
    "Pts+Reb":       lambda g: parse_float(g.get("points",0)) + parse_float(g.get("totalRebounds",0)),
    "Reb+Ast":       lambda g: parse_float(g.get("totalRebounds",0)) + parse_float(g.get("assists",0)),
    "Fantasy Score": lambda g: (
        parse_float(g.get("points",0)) + parse_float(g.get("totalRebounds",0))*1.2
        + parse_float(g.get("assists",0))*1.5 + parse_float(g.get("steals",0))*3
        + parse_float(g.get("blocks",0))*3 - parse_float(g.get("turnovers",0))
    ),
}


# ============================================================
#  FOLDER SCANNING
# ============================================================

def find_unresolved_folders(root: Path, hours_threshold: float = 24.0) -> List[Path]:
    """
    Find all dated folders with predictions but no resolved results,
    where the earliest game started more than hours_threshold hours ago.
    """
    now       = datetime.now(timezone.utc)
    cutoff    = now - timedelta(hours=hours_threshold)
    eligible  = []

    for folder in sorted(root.iterdir()):
        if not folder.is_dir():
            continue
        if not re.match(r"^\d{8}-\w+$", folder.name):
            continue

        predictions_path = folder / "confidence_results.json"
        resolved_path    = folder / "actual_results.json"

        if not predictions_path.exists():
            continue
        if resolved_path.exists():
            continue

        # Check if games have had time to finish
        with predictions_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)

        start_times = [
            _parse_start_time(r.get("start_time", ""))
            for r in data.get("results", [])
            if r.get("start_time")
        ]
        valid_times = [t for t in start_times if t is not None]

        if not valid_times:
            print(f"  [SKIP] {folder.name} — no valid start times found", file=sys.stderr)
            continue

        earliest = min(valid_times)
        if earliest > cutoff:
            remaining = (earliest + timedelta(hours=hours_threshold)) - now
            hrs  = int(remaining.total_seconds() // 3600)
            mins = int((remaining.total_seconds() % 3600) // 60)
            print(
                f"  [SKIP] {folder.name} — games not finished yet "
                f"(eligible in ~{hrs}h {mins}m)",
                file=sys.stderr,
            )
            continue

        eligible.append(folder)

    return eligible


# ============================================================
#  MLB RESOLUTION
# ============================================================

def mlb_fetch_game_stat(
    player_id: int,
    game_date: str,
    stat_type: str,
    is_pitcher: bool,
    insecure: bool,
) -> Optional[float]:
    """Fetch the actual stat value for a player on a specific date."""
    group    = "pitching" if is_pitcher else "hitting"
    url      = f"{MLB_PEOPLE_BASE}/{player_id}/stats"
    season   = int(game_date[:4]) if game_date else 2026

    try:
        data   = fetch_json(url, params={"stats": "gameLog", "season": season, "group": group}, insecure=insecure)
        splits = data.get("stats", [{}])[0].get("splits", [])
    except Exception as e:
        print(f"    [WARN] MLB API error for player {player_id}: {e}", file=sys.stderr)
        return None

    extractors = MLB_PITCHER_EXTRACTORS if is_pitcher else MLB_HITTER_EXTRACTORS
    extractor  = extractors.get(stat_type)
    if not extractor:
        return None

    # Match by date (format: YYYY-MM-DD)
    for split in splits:
        split_date = split.get("date", "")[:10]
        if split_date == game_date[:10]:
            return extractor(split.get("stat", {}))

    return None


def mlb_load_active_roster(insecure: bool) -> List[Dict]:
    """
    Fetch active MLB roster using the people/search endpoint.
    More reliable than the season roster for current-year resolution.
    """
    # Use all teams endpoint to get full 40-man rosters
    teams_url = "https://statsapi.mlb.com/api/v1/teams"
    try:
        teams_data = fetch_json(teams_url, params={"sportId": 1, "season": 2026}, insecure=insecure)
        team_ids   = [str(t["id"]) for t in teams_data.get("teams", [])]
    except Exception as e:
        print(f"  [WARN] Could not fetch team list: {e}", file=sys.stderr)
        # Fallback to season roster endpoint
        url  = "https://statsapi.mlb.com/api/v1/sports/1/players"
        data = fetch_json(url, params={"season": 2026, "sportId": 1}, insecure=insecure)
        return data.get("people", [])

    all_players = []
    for tid in team_ids:
        try:
            roster_url  = f"https://statsapi.mlb.com/api/v1/teams/{tid}/roster"
            roster_data = fetch_json(roster_url, params={"rosterType": "40Man"}, insecure=insecure)
            for entry in roster_data.get("roster", []):
                p = entry.get("person", {})
                if p.get("id"):
                    all_players.append({
                        "id":       p["id"],
                        "fullName": p.get("fullName", ""),
                    })
        except Exception:
            continue

    print(f"  Loaded {len(all_players)} players from 40-man rosters.", file=sys.stderr)
    return all_players


def mlb_resolve_player_ids_local(
    names: List[str],
    all_players: List[Dict],
) -> Dict[str, int]:
    """Build name -> player_id mapping with fuzzy matching."""
    index: Dict[str, List[Dict]] = {}
    for p in all_players:
        key = normalize_name(p.get("fullName", ""))
        if key:
            index.setdefault(key, []).append(p)

    resolved: Dict[str, int] = {}
    for name in names:
        key        = normalize_name(name)
        candidates = index.get(key, [])
        if not candidates:
            # Fuzzy fallback: try starts-with match
            for k, v in index.items():
                if k.startswith(key[:6]) or key.startswith(k[:6]):
                    candidates = v
                    break
        if candidates:
            resolved[name] = int(candidates[0]["id"])
        else:
            print(f"  [WARN] MLB: could not resolve '{name}'", file=sys.stderr)

    return resolved


def mlb_resolve_folder(
    folder: Path,
    predictions: List[Dict],
    mlb_id_cache: Dict[str, int],
    insecure: bool,
) -> List[Dict]:
    """Resolve MLB predictions against actual outcomes."""
    player_names = list({p["player"] for p in predictions})

    # Build player ID cache if needed
    missing = [n for n in player_names if n not in mlb_id_cache]
    if missing:
        print(f"  Loading MLB roster for {len(missing)} unresolved players...", file=sys.stderr)
        all_mlb  = mlb_load_active_roster(insecure)
        resolved = mlb_resolve_player_ids_local(missing, all_mlb)
        mlb_id_cache.update(resolved)
        matched   = sum(1 for n in missing if n in mlb_id_cache)
        unmatched = len(missing) - matched
        print(f"  Resolved {matched}/{len(missing)} players ({unmatched} unmatched).", file=sys.stderr)

    # Determine pitchers from predictions
    pitcher_stat_types = set(MLB_PITCHER_EXTRACTORS.keys())
    results = []

    for pred in predictions:
        player    = pred["player"]
        stat_type = pred["stat_type"]
        line      = pred["line"]
        game_date = pred.get("start_time", "")[:10]
        player_id = mlb_id_cache.get(player)

        if not player_id or not game_date:
            results.append({**pred, "actual": None, "hit": None, "resolved": False})
            continue

        is_pitcher = stat_type in pitcher_stat_types
        actual     = mlb_fetch_game_stat(player_id, game_date, stat_type, is_pitcher, insecure)

        results.append({
            **pred,
            "actual":   actual,
            "hit":      bool(actual is not None and actual > line),
            "resolved": actual is not None,
        })

    return results


# ============================================================
#  NBA RESOLUTION
# ============================================================

# ============================================================
#  WRITE OUTPUT
# ============================================================

def write_actual_results(folder: Path, results: List[Dict], sport: str) -> None:
    resolved   = [r for r in results if r.get("resolved")]
    total      = len(results)
    n_resolved = len(resolved)
    n_hit      = sum(1 for r in resolved if r.get("hit"))

    payload = {
        "resolved_at":    datetime.now().isoformat(),
        "sport":          sport.upper(),
        "total_props":    total,
        "resolved_props": n_resolved,
        "hits":           n_hit,
        "misses":         n_resolved - n_hit,
        "accuracy":       round(n_hit / n_resolved, 4) if n_resolved else None,
        "results":        results,
    }

    out_path = folder / "actual_results.json"
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)

    print(
        f"  Saved → {out_path.name}  "
        f"({n_resolved}/{total} resolved, accuracy: "
        f"{payload['accuracy']*100:.1f}% [{n_hit}/{n_resolved}])"
        if payload["accuracy"] is not None else
        f"  Saved → {out_path.name}  ({n_resolved}/{total} resolved)",
        file=sys.stderr,
    )


# ============================================================
#  MAIN RESOLUTION LOOP
# ============================================================

def resolve_folder(folder: Path, insecure: bool) -> bool:
    sport            = folder.name.split("-")[-1].lower()
    predictions_path = folder / "confidence_results.json"

    with predictions_path.open("r", encoding="utf-8") as fh:
        data        = json.load(fh)
    predictions = data.get("results", [])

    if not predictions:
        print(f"  [SKIP] {folder.name} — no predictions found", file=sys.stderr)
        return False

    print(f"\n  Resolving {folder.name} ({len(predictions)} props, sport={sport.upper()})...", file=sys.stderr)

    if sport != "mlb":
        print(f"  [SKIP] Only MLB is supported. Folder: {folder.name}", file=sys.stderr)
        return False

    results = mlb_resolve_folder(folder, predictions, {}, insecure)

    write_actual_results(folder, results, sport)
    return True


# ============================================================
#  CLI
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve PrizePicks predictions against actual game outcomes.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--folder",
        default=None,
        help="Resolve a single specific folder. If omitted, auto-scans all unresolved folders.",
    )
    parser.add_argument(
        "--output-root",
        default=".",
        help="Root directory to scan for dated folders (default: current dir).",
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=24.0,
        help="Hours after earliest game before a folder is eligible for resolution (default: 24).",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable SSL certificate verification.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.folder:
        folders = [Path(args.folder)]
    else:
        print(f"\nScanning for unresolved prediction folders...", file=sys.stderr)
        folders = find_unresolved_folders(Path(args.output_root), args.hours)
        if not folders:
            print("  No folders ready for resolution.", file=sys.stderr)
            return
        print(f"  Found {len(folders)} folder(s) to resolve.", file=sys.stderr)

    resolved_count = 0
    for folder in folders:
        if resolve_folder(folder, args.insecure):
            resolved_count += 1

    print(f"\nDone. {resolved_count}/{len(folders)} folder(s) resolved.", file=sys.stderr)
    if resolved_count:
        print("Next step: python train_model.py", file=sys.stderr)


if __name__ == "__main__":
    main()