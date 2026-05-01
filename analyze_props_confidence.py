"""
analyze_props_confidence.py

PrizePicks prop confidence analyzer — NBA and MLB.

Reads the most recent dated folder (MMDDYYYY) for a PrizePicks CSV export,
fetches full season game logs for every player in the CSV automatically,
scores every prop line against historical performance, and writes a ranked
JSON output file to the same dated folder.

No manual player filtering — every player on the board gets scored.

Outputs:
  <dated_folder>/confidence_results.json

Usage
-----
# Auto-detect most recent dated folder, prompt for sport
python analyze_props_confidence.py

# Explicit sport
python analyze_props_confidence.py --sport mlb
python analyze_props_confidence.py --sport nba

# Explicit folder override
python analyze_props_confidence.py --sport mlb --folder ./04282026-MLB
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from requests.exceptions import SSLError
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ============================================================
#  SHARED UTILITIES
# ============================================================

def normalize_name(name: str) -> str:
    replacements = {
        "\u00e1": "a", "\u00e0": "a", "\u00e2": "a", "\u00e4": "a", "\u00e3": "a",
        "\u00e9": "e", "\u00e8": "e", "\u00ea": "e", "\u00eb": "e",
        "\u00ed": "i", "\u00ec": "i", "\u00ee": "i", "\u00ef": "i",
        "\u00f3": "o", "\u00f2": "o", "\u00f4": "o", "\u00f6": "o", "\u00f5": "o",
        "\u00fa": "u", "\u00f9": "u", "\u00fb": "u", "\u00fc": "u",
        "\u00f1": "n", "\u00e7": "c", "\u010d": "c", "\u0107": "c",
        "\u017e": "z", "\u0161": "s", "\u00fd": "y",
    }
    name = name.lower()
    for src, dst in replacements.items():
        name = name.replace(src, dst)
    return re.sub(r"[^a-z0-9]", "", name)


def parse_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def fetch_json(
    url: str,
    params: Optional[Dict] = None,
    insecure: bool = False,
) -> Dict[str, Any]:
    try:
        r = requests.get(url, params=params, timeout=30, verify=not insecure)
        r.raise_for_status()
        return r.json()
    except SSLError:
        if insecure:
            raise
        r = requests.get(url, params=params, timeout=30, verify=False)
        r.raise_for_status()
        return r.json()


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


# ============================================================
#  FOLDER AUTO-DETECTION
# ============================================================

def find_latest_folder(root: Path, sport: Optional[str] = None) -> tuple:
    """
    Find the most recently created MMDDYYYY-SPORT folder under root.
    Returns (folder_path, detected_sport).
    Sport is read directly from the folder name — no prompt needed.
    """
    all_dated = sorted(
        [p for p in root.iterdir() if p.is_dir() and re.match(r"^\d{8}-\w+$", p.name)],
        reverse=True,
    )

    if not all_dated:
        raise FileNotFoundError(
            f"No dated folder found under {root}. "
            "Run prizepicks_export.py first to generate a CSV."
        )

    if sport:
        matches = [p for p in all_dated if p.name.upper().endswith(f"-{sport.upper()}")]
        folder  = matches[0] if matches else all_dated[0]
    else:
        folder = all_dated[0]

    detected_sport = folder.name.split("-")[-1].lower()
    return folder, detected_sport


def find_csv_in_folder(folder: Path) -> Path:
    csvs = list(folder.glob("*.csv"))
    if not csvs:
        raise FileNotFoundError(f"No CSV file found in {folder}")
    return csvs[0]


# ============================================================
#  CSV READING — all players, all props
# ============================================================

TIER_PRIORITY = {"standard": 0, "demon": 1, "goblin": 2}


def read_all_players_from_csv(csv_path: Path) -> Tuple[List[str], List[str]]:
    """
    Returns (all_player_names, pitcher_names).
    Skips combo rows and already-started games.
    """
    now = datetime.now(timezone.utc)
    seen: Dict[str, str] = {}
    skipped = 0

    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            name = (row.get("player") or "").strip()
            if not name or "+" in name:
                continue
            start_dt = _parse_start_time((row.get("start_time") or "").strip())
            if start_dt is not None and start_dt <= now:
                skipped += 1
                continue
            if name not in seen:
                seen[name] = (row.get("position") or "").strip().upper()

    if skipped:
        print(f"  [INFO] Skipped {skipped} rows for games already started.", file=sys.stderr)

    all_players   = list(seen.keys())
    pitcher_names = [n for n, pos in seen.items() if pos == "P"]

    print(
        f"  [CSV] {len(all_players)} players found "
        f"({len(pitcher_names)} pitchers, {len(all_players) - len(pitcher_names)} hitters).",
        file=sys.stderr,
    )
    return all_players, pitcher_names


def load_all_lines(
    csv_path: Path,
    players: List[str],
    valid_stat_types: set,
) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    """
    Returns all future prop lines grouped by (player, stat_type).
    Sorted: standard tier first, then ascending line value.
    """
    player_lookup = {normalize_name(p): p for p in players}
    now = datetime.now(timezone.utc)
    collected: Dict[Tuple[str, str], List[Dict]] = defaultdict(list)

    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            csv_player = (row.get("player") or "").strip()
            if not csv_player or "+" in csv_player:
                continue
            start_dt = _parse_start_time((row.get("start_time") or "").strip())
            if start_dt is not None and start_dt <= now:
                continue
            norm = normalize_name(csv_player)
            if norm not in player_lookup:
                continue
            stat_type = (row.get("stat_type") or "").strip()
            if stat_type not in valid_stat_types:
                continue

            odds_type      = (row.get("odds_type") or "").strip().lower()
            line           = parse_float(row.get("line"))
            team           = (row.get("team") or "").strip()
            start_time_raw = (row.get("start_time") or "").strip()
            date_part      = start_time_raw[:10] if start_time_raw else "unknown"
            game_key       = f"{team}_{date_part}" if team else date_part
            opponent       = (row.get("opponent") or "").strip()

            collected[(player_lookup[norm], stat_type)].append({
                "line":       line,
                "odds_type":  odds_type,
                "start_time": start_time_raw,
                "game_key":   game_key,
                "opponent":   opponent,
                "team":       team,
            })

    result: Dict[Tuple[str, str], List[Dict]] = {}
    for key, rows in collected.items():
        rows.sort(key=lambda r: (TIER_PRIORITY.get(r["odds_type"], 99), r["line"]))
        result[key] = rows

    return result


def select_best_line(lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not lines:
        return {}
    for line in lines:
        if line["odds_type"] == "standard":
            return line
    return lines[1] if len(lines) > 1 else lines[0]


# ============================================================
#  RESULT BUILDING
# ============================================================

# ── Confidence tuning ───────────────────────────────────────
# Default parameters — overridden by model_params.json if present.
PRIOR_WEIGHT    = 10.0
AVG_FACTOR_MIN  = 0.67
AVG_FACTOR_MAX  = 1.50


def _load_model_params(root: Path = Path(".")) -> None:
    """
    Load tuned parameters from model_params.json if it exists.
    Prints a full startup summary showing model version, training history,
    current parameters, and whether the feedback loop is active.
    """
    global PRIOR_WEIGHT, AVG_FACTOR_MIN, AVG_FACTOR_MAX

    sep = "=" * 50
    print(f"\n  {sep}", file=sys.stderr)
    print(f"  MLB Prop Confidence Analyzer — Model Status", file=sys.stderr)
    print(f"  {sep}", file=sys.stderr)

    params_path = root / "model_params.json"
    perf_path   = root / "model_performance.json"

    if params_path.exists():
        try:
            with params_path.open("r", encoding="utf-8") as fh:
                params = json.load(fh)

            PRIOR_WEIGHT   = float(params.get("prior_weight",   PRIOR_WEIGHT))
            AVG_FACTOR_MIN = float(params.get("avg_factor_min", AVG_FACTOR_MIN))
            AVG_FACTOR_MAX = float(params.get("avg_factor_max", AVG_FACTOR_MAX))

            v              = params.get("version",       1)
            sessions_used  = params.get("sessions_used", 0)
            trained_on     = params.get("trained_on",    None)
            notes          = params.get("notes",         "")

            # Format trained_on date nicely
            if trained_on:
                try:
                    dt = datetime.fromisoformat(trained_on)
                    trained_str = dt.strftime("%Y-%m-%d %I:%M %p")
                except Exception:
                    trained_str = trained_on
            else:
                trained_str = "never"

            print(f"  Status        : ✓ TRAINED MODEL ACTIVE", file=sys.stderr)
            print(f"  Version       : v{v}", file=sys.stderr)
            print(f"  Last trained  : {trained_str}", file=sys.stderr)
            print(f"  Sessions used : {sessions_used}", file=sys.stderr)
            print(f"  Prior weight  : {PRIOR_WEIGHT}  (default: 10.0)", file=sys.stderr)
            print(f"  Avg factor    : [{AVG_FACTOR_MIN}, {AVG_FACTOR_MAX}]  (default: [0.67, 1.5])", file=sys.stderr)
            print(f"  Notes         : {notes}", file=sys.stderr)

            # Load performance summary if available
            if perf_path.exists():
                try:
                    with perf_path.open("r", encoding="utf-8") as fh:
                        perf = json.load(fh)
                    analysis = perf.get("analysis", {})
                    overall  = analysis.get("overall_accuracy")
                    total    = analysis.get("total_props", 0)
                    adj_err  = analysis.get("adj_calibration_error")
                    raw_err  = analysis.get("raw_calibration_error")
                    adj_wins = analysis.get("adj_beats_raw", False)
                    if overall is not None:
                        print(f"\n  --- Performance Summary ---", file=sys.stderr)
                        print(f"  Overall accuracy  : {overall*100:.1f}% ({total} props resolved)", file=sys.stderr)
                        print(f"  Adj calibration   : {adj_err:.4f}" if adj_err else "  Adj calibration   : —", file=sys.stderr)
                        print(f"  Raw calibration   : {raw_err:.4f}" if raw_err else "  Raw calibration   : —", file=sys.stderr)
                        winner = "Adjusted ✓" if adj_wins else "Raw ✓"
                        print(f"  Better predictor  : {winner}", file=sys.stderr)
                except Exception:
                    pass

        except Exception as e:
            print(f"  Status  : ✗ Could not load model_params.json: {e}", file=sys.stderr)
            print(f"  Falling back to default parameters.", file=sys.stderr)
    else:
        print(f"  Status        : ○ NO TRAINED MODEL — using defaults", file=sys.stderr)
        print(f"  Prior weight  : {PRIOR_WEIGHT}", file=sys.stderr)
        print(f"  Avg factor    : [{AVG_FACTOR_MIN}, {AVG_FACTOR_MAX}]", file=sys.stderr)
        print(f"\n  To activate the feedback loop:", file=sys.stderr)
        print(f"    1. Run resolve_results.py after games finish", file=sys.stderr)
        print(f"    2. Run train_model.py to generate model_params.json", file=sys.stderr)

    print(f"  {sep}\n", file=sys.stderr)


def _adjusted_confidence(hits: int, games: int, avg: float, line: float) -> float:
    """
    Bayesian-shrinkage confidence with season-average reinforcement.

    Step 1 — Shrink raw hit rate toward 0.5 based on sample size:
        adjusted = (hits + PRIOR_WEIGHT * 0.5) / (games + PRIOR_WEIGHT)

    Step 2 — Apply avg-vs-line factor (capped at 1.5x, floored at 0.67x):
        avg_factor = clamp(avg / line, 0.67, 1.5)   if line > 0 else 1.0
        final      = clamp(adjusted * avg_factor, 0.0, 1.0)

    Returns raw_confidence, adjusted_confidence as a tuple.
    """
    raw = round(hits / games, 4) if games else 0.0

    # Bayesian shrinkage
    shrunk = (hits + PRIOR_WEIGHT * 0.5) / (games + PRIOR_WEIGHT)

    # Season average reinforcement
    if line > 0 and avg > 0:
        avg_factor = min(max(avg / line, AVG_FACTOR_MIN), AVG_FACTOR_MAX)
    else:
        avg_factor = 1.0

    adjusted = round(min(max(shrunk * avg_factor, 0.0), 1.0), 4)
    return raw, adjusted


def _append_result(
    results: List[Dict[str, Any]],
    player: str,
    stat_type: str,
    sport: str,
    line_info: Dict[str, Any],
    game_logs: List[Dict],
    extractor: Callable,
) -> None:
    values = [extractor(g) for g in game_logs]
    line   = line_info["line"]
    hits   = sum(1 for v in values if v > line)
    games  = len(values)
    avg    = round(sum(values) / games, 2) if games else 0.0

    raw_conf, adj_conf = _adjusted_confidence(hits, games, avg, line)

    results.append({
        "player":             player,
        "sport":              sport.upper(),
        "stat_type":          stat_type,
        "line":               line,
        "confidence":         adj_conf,   # adjusted — used for sorting/slates
        "confidence_raw":     raw_conf,   # raw hit rate for reference
        "hits":               hits,
        "games":              games,
        "average":            avg,
        "odds_type":          line_info.get("odds_type",  ""),
        "start_time":         line_info.get("start_time", ""),
        "game_key":           line_info.get("game_key",   ""),
        "opponent":           line_info.get("opponent",   ""),
        "team":               line_info.get("team",       ""),
    })


# ============================================================
#  MLB  (MLB Stats API)
# ============================================================

MLB_SPORTS_PLAYERS = "https://statsapi.mlb.com/api/v1/sports/1/players"
MLB_PEOPLE_BASE    = "https://statsapi.mlb.com/api/v1/people"

MLB_HITTER_EXTRACTORS: Dict[str, Callable[[Dict], float]] = {
    "Hits":                 lambda s: parse_float(s.get("hits",          0)),
    "Runs":                 lambda s: parse_float(s.get("runs",          0)),
    "RBIs":                 lambda s: parse_float(s.get("rbi",           0)),
    "Home Runs":            lambda s: parse_float(s.get("homeRuns",      0)),
    "Stolen Bases":         lambda s: parse_float(s.get("stolenBases",   0)),
    "Total Bases":          lambda s: parse_float(s.get("totalBases",    0)),
    "Doubles":              lambda s: parse_float(s.get("doubles",       0)),
    "Triples":              lambda s: parse_float(s.get("triples",       0)),
    "Walks":                lambda s: parse_float(s.get("baseOnBalls",   0)),
    "Hitter Strikeouts":    lambda s: parse_float(s.get("strikeOuts",    0)),
    "Singles":              lambda s: (
        parse_float(s.get("hits",     0))
        - parse_float(s.get("doubles",  0))
        - parse_float(s.get("triples",  0))
        - parse_float(s.get("homeRuns", 0))
    ),
    "Hits+Runs+RBIs":       lambda s: (
        parse_float(s.get("hits",  0))
        + parse_float(s.get("runs", 0))
        + parse_float(s.get("rbi",  0))
    ),
    "Hitter Fantasy Score": lambda s: (
        parse_float(s.get("hits",          0)) * 3
        + parse_float(s.get("runs",        0)) * 2
        + parse_float(s.get("rbi",         0)) * 2
        + parse_float(s.get("baseOnBalls", 0)) * 2
        + parse_float(s.get("stolenBases", 0)) * 3
        + parse_float(s.get("homeRuns",    0)) * 3
        - parse_float(s.get("strikeOuts",  0))
    ),
}

MLB_PITCHER_EXTRACTORS: Dict[str, Callable[[Dict], float]] = {
    "Pitcher Strikeouts":   lambda s: parse_float(s.get("strikeOuts",       0)),
    "Hits Allowed":         lambda s: parse_float(s.get("hits",             0)),
    "Earned Runs Allowed":  lambda s: parse_float(s.get("earnedRuns",       0)),
    "Walks Allowed":        lambda s: parse_float(s.get("baseOnBalls",      0)),
    "Pitches Thrown":       lambda s: parse_float(s.get("numberOfPitches",  0)),
    "Home Runs Allowed":    lambda s: parse_float(s.get("homeRuns",         0)),
    "Pitching Outs":        lambda s: _outs_from_innings(s.get("inningsPitched", 0)),
    "Pitcher Fantasy Score":lambda s: (
        parse_float(s.get("strikeOuts",     0)) * 3
        - parse_float(s.get("hits",         0)) * 0.6
        - parse_float(s.get("earnedRuns",   0)) * 3
        - parse_float(s.get("baseOnBalls",  0)) * 0.6
        + _outs_from_innings(s.get("inningsPitched", 0)) * 0.5
    ),
}

MLB_ALL_EXTRACTORS = {**MLB_HITTER_EXTRACTORS, **MLB_PITCHER_EXTRACTORS}


def _outs_from_innings(ip_str: Any) -> float:
    try:
        ip        = float(ip_str)
        whole     = int(ip)
        remainder = round((ip - whole) * 10)
        return whole * 3 + remainder
    except (TypeError, ValueError):
        return 0.0


def mlb_load_all_players(season: int, insecure: bool) -> List[Dict[str, Any]]:
    data = fetch_json(
        MLB_SPORTS_PLAYERS,
        params={"season": season, "sportId": 1},
        insecure=insecure,
    )
    return data.get("people", [])


def mlb_resolve_player_ids(
    names: List[str],
    all_players: List[Dict],
) -> Dict[str, int]:
    index: Dict[str, List[Dict]] = {}
    for p in all_players:
        full = p.get("fullName") or p.get("boxscoreName") or ""
        key  = normalize_name(full)
        if key:
            index.setdefault(key, []).append(p)
    resolved: Dict[str, int] = {}
    for name in names:
        key        = normalize_name(name)
        candidates = index.get(key, [])
        if not candidates:
            for k, v in index.items():
                if k.startswith(key) or key.startswith(k):
                    candidates = v
                    break
        if candidates:
            resolved[name] = int(candidates[0]["id"])
        else:
            print(f"  [WARN] MLB: could not resolve '{name}'", file=sys.stderr)
    return resolved


def mlb_fetch_hitting_log(
    player_id: int,
    season: int,
    insecure: bool,
) -> List[Dict]:
    url  = f"{MLB_PEOPLE_BASE}/{player_id}/stats"
    data = fetch_json(
        url,
        params={"stats": "gameLog", "season": season, "group": "hitting"},
        insecure=insecure,
    )
    return [s.get("stat", {}) for s in data.get("stats", [{}])[0].get("splits", [])]


def mlb_fetch_pitching_log(
    player_id: int,
    season: int,
    insecure: bool,
) -> List[Dict]:
    url  = f"{MLB_PEOPLE_BASE}/{player_id}/stats"
    data = fetch_json(
        url,
        params={"stats": "gameLog", "season": season, "group": "pitching"},
        insecure=insecure,
    )
    return [s.get("stat", {}) for s in data.get("stats", [{}])[0].get("splits", [])]


def mlb_run(
    csv_path: Path,
    all_players: List[str],
    pitcher_names: List[str],
    season: int,
    insecure: bool,
) -> List[Dict[str, Any]]:
    hitter_names = [p for p in all_players if p not in pitcher_names]
    all_lines    = load_all_lines(csv_path, all_players, set(MLB_ALL_EXTRACTORS))
    best_lines   = {k: select_best_line(v) for k, v in all_lines.items()}

    print("Loading MLB player roster from MLB Stats API...", file=sys.stderr)
    all_mlb = mlb_load_all_players(season, insecure)
    print(f"  {len(all_mlb)} active players loaded.", file=sys.stderr)

    resolved_hitters  = mlb_resolve_player_ids(hitter_names,  all_mlb) if hitter_names  else {}
    resolved_pitchers = mlb_resolve_player_ids(pitcher_names, all_mlb) if pitcher_names else {}

    results: List[Dict[str, Any]] = []

    for player, pid in resolved_hitters.items():
        print(f"  Fetching hitting log: {player}...", file=sys.stderr)
        logs = mlb_fetch_hitting_log(pid, season, insecure)
        if not logs:
            print(f"  [WARN] No hitting data for {player}", file=sys.stderr)
            continue
        for (lp, stat_type), line_info in best_lines.items():
            if lp != player or not line_info:
                continue
            extractor = MLB_HITTER_EXTRACTORS.get(stat_type)
            if extractor is None:
                continue
            _append_result(results, player, stat_type, "mlb", line_info, logs, extractor)

    for player, pid in resolved_pitchers.items():
        print(f"  Fetching pitching log: {player}...", file=sys.stderr)
        logs = mlb_fetch_pitching_log(pid, season, insecure)
        if not logs:
            print(f"  [WARN] No pitching data for {player}", file=sys.stderr)
            continue
        for (lp, stat_type), line_info in best_lines.items():
            if lp != player or not line_info:
                continue
            extractor = MLB_PITCHER_EXTRACTORS.get(stat_type)
            if extractor is None:
                continue
            _append_result(results, player, stat_type, "mlb", line_info, logs, extractor)

    results.sort(key=lambda x: (x["confidence"], x["games"], x["average"]), reverse=True)
    return results


# ============================================================
#  OUTPUT
# ============================================================

def write_results(folder: Path, results: List[Dict], sport: str, season: int) -> Path:
    out_path = folder / "confidence_results.json"
    payload = {
        "generated_at": datetime.now().isoformat(),
        "sport":        sport.upper(),
        "season":       season,
        "total_props":  len(results),
        "results":      results,
    }
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"\nResults saved → {out_path}", file=sys.stderr)
    return out_path


# ============================================================
#  CLI
# ============================================================

# Sport is fixed to MLB — prompt removed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PrizePicks prop confidence analyzer — NBA and MLB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # Sport is fixed to MLB
    # parser.add_argument("--sport", ...)  # reserved for future use
    parser.add_argument(
        "--folder",
        default=None,
        help="Path to dated folder containing the PrizePicks CSV. "
             "Auto-detected from current directory if omitted.",
    )
    parser.add_argument(
        "--output-root",
        default=".",
        help="Root directory to search for dated folders (default: current directory).",
    )
    parser.add_argument(
        "--season",
        type=int,
        default=2026,
        help="Season year (default: 2026).",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable SSL certificate verification.",
    )
    return parser.parse_args()


# ============================================================
#  MAIN
# ============================================================

def main() -> None:
    args = parse_args()

    # Resolve folder and auto-detect sport from folder name
    if args.folder:
        folder = Path(args.folder)
        sport  = folder.name.split("-")[-1].lower()
    else:
        folder, detected = find_latest_folder(Path(args.output_root))
        sport = detected

    if sport != "mlb":
        print(f"  [ERROR] Could not determine sport from folder '{folder.name}'.", file=sys.stderr)
        print("  Folder must be named MMDDYYYY-MLB.", file=sys.stderr)
        sys.exit(1)

    print(f"\n  Sport: {sport.upper()} (detected from folder)", file=sys.stderr)
    print(f"  Using folder: {folder}", file=sys.stderr)

    # Load tuned model parameters if available
    _load_model_params(Path(args.output_root))
    csv_path = find_csv_in_folder(folder)
    print(f"  CSV: {csv_path.name}", file=sys.stderr)

    # Read all players from CSV
    all_players, pitcher_names = read_all_players_from_csv(csv_path)

    if not all_players:
        print("\n  [ERROR] No upcoming players found in CSV.", file=sys.stderr)
        print("  All games may have already started — re-run prizepicks_export.py first.", file=sys.stderr)
        sys.exit(1)

    # Run sport-specific analysis
    if sport == "mlb":
        results = mlb_run(
            csv_path=csv_path,
            all_players=all_players,
            pitcher_names=pitcher_names,
            season=args.season,
            insecure=args.insecure,
        )


    if not results:
        print("\n  [ERROR] No results generated. Check player names and CSV content.", file=sys.stderr)
        sys.exit(1)

    print(f"\n  {len(results)} props scored.", file=sys.stderr)

    # Write output
    write_results(folder, results, sport, args.season)


if __name__ == "__main__":
    main()
