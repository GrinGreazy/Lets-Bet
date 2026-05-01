"""
train_model.py

Reads all resolved prediction folders (those containing both
confidence_results.json and actual_results.json), calculates
prediction accuracy across sport, stat type, and confidence tier,
and writes updated model parameters to model_params.json.

Also writes model_performance.json — a full historical record of
accuracy metrics that viz_props.py uses to render the performance dashboard.

Usage
-----
python train_model.py              # scan all resolved folders
python train_model.py --output-root ./data
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ============================================================
#  DEFAULT MODEL PARAMETERS
#  These are the starting values before any training data exists.
#  train_model.py will adjust them based on observed accuracy.
# ============================================================

DEFAULT_PARAMS = {
    "prior_weight":    10.0,   # Bayesian shrinkage strength
    "avg_factor_min":  0.67,   # Floor for avg-vs-line multiplier
    "avg_factor_max":  1.50,   # Cap for avg-vs-line multiplier
    "min_games_warn":  5,      # Sample size below which ⚠ is shown
    "version":         1,
    "trained_on":      None,
    "sessions_used":   0,
    "notes":           "Default parameters — no training data yet.",
}


# ============================================================
#  FOLDER SCANNING
# ============================================================

def find_resolved_folders(root: Path) -> List[Path]:
    folders = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir():
            continue
        if not re.match(r"^\d{8}-\w+$", folder.name):
            continue
        if (folder / "confidence_results.json").exists() and \
           (folder / "actual_results.json").exists():
            folders.append(folder)
    return folders


# ============================================================
#  DATA LOADING
# ============================================================

def load_session(folder: Path) -> Optional[Dict]:
    """Load and merge predictions with actuals for one session."""
    with (folder / "confidence_results.json").open("r", encoding="utf-8") as fh:
        predictions = json.load(fh)
    with (folder / "actual_results.json").open("r", encoding="utf-8") as fh:
        actuals = json.load(fh)

    # Build lookup: (player, stat_type) -> actual result
    actual_lookup: Dict[Tuple[str, str], Dict] = {}
    for r in actuals.get("results", []):
        if r.get("resolved"):
            key = (r["player"], r["stat_type"])
            actual_lookup[key] = r

    merged = []
    for pred in predictions.get("results", []):
        key    = (pred["player"], pred["stat_type"])
        actual = actual_lookup.get(key)
        if actual is None:
            continue
        merged.append({
            "player":          pred["player"],
            "sport":           pred.get("sport", ""),
            "stat_type":       pred["stat_type"],
            "line":            pred["line"],
            "confidence":      pred["confidence"],
            "confidence_raw":  pred.get("confidence_raw", pred["confidence"]),
            "games":           pred.get("games", 0),
            "average":         pred.get("average", 0),
            "odds_type":       pred.get("odds_type", ""),
            "actual":          actual.get("actual"),
            "hit":             actual.get("hit"),
            "folder":          folder.name,
            "date":            folder.name[:8],
        })

    return {
        "folder":   folder.name,
        "sport":    actuals.get("sport", ""),
        "date":     folder.name[:8],
        "accuracy": actuals.get("accuracy"),
        "resolved": actuals.get("resolved_props", 0),
        "total":    actuals.get("total_props", 0),
        "props":    merged,
    }


# ============================================================
#  ACCURACY ANALYSIS
# ============================================================

def confidence_bucket(conf: float) -> str:
    if conf >= 0.80:
        return "80-100%"
    elif conf >= 0.70:
        return "70-79%"
    elif conf >= 0.60:
        return "60-69%"
    elif conf >= 0.50:
        return "50-59%"
    else:
        return "<50%"


def analyze_sessions(sessions: List[Dict]) -> Dict:
    """
    Compute accuracy breakdowns across all sessions.
    Returns structured metrics for parameter tuning and dashboard display.
    """
    all_props = [p for s in sessions for p in s["props"]]

    if not all_props:
        return {}

    # Overall accuracy — adjusted vs raw confidence
    def bucket_accuracy(props, conf_key):
        buckets = defaultdict(lambda: {"hits": 0, "total": 0})
        for p in props:
            b = confidence_bucket(p.get(conf_key, 0))
            buckets[b]["total"] += 1
            if p.get("hit"):
                buckets[b]["hits"] += 1
        return {
            b: {
                "hits":     v["hits"],
                "total":    v["total"],
                "accuracy": round(v["hits"] / v["total"], 4) if v["total"] else None,
            }
            for b, v in sorted(buckets.items())
        }

    # By sport
    sport_acc = defaultdict(lambda: {"hits": 0, "total": 0})
    for p in all_props:
        sport_acc[p["sport"]]["total"] += 1
        if p.get("hit"):
            sport_acc[p["sport"]]["hits"] += 1

    # By stat type
    stat_acc = defaultdict(lambda: {"hits": 0, "total": 0})
    for p in all_props:
        stat_acc[p["stat_type"]]["total"] += 1
        if p.get("hit"):
            stat_acc[p["stat_type"]]["hits"] += 1

    # By tier
    tier_acc = defaultdict(lambda: {"hits": 0, "total": 0})
    for p in all_props:
        tier_acc[p.get("odds_type", "unknown")]["total"] += 1
        if p.get("hit"):
            tier_acc[p.get("odds_type", "unknown")]["hits"] += 1

    # Adjusted vs raw — which predicts better?
    def calibration_error(props, conf_key):
        """Mean absolute difference between predicted confidence and actual hit rate per bucket."""
        buckets = defaultdict(lambda: {"conf_sum": 0.0, "hits": 0, "total": 0})
        for p in props:
            b = confidence_bucket(p.get(conf_key, 0))
            buckets[b]["conf_sum"] += p.get(conf_key, 0)
            buckets[b]["total"]    += 1
            if p.get("hit"):
                buckets[b]["hits"] += 1
        errors = []
        for v in buckets.values():
            if v["total"] > 0:
                avg_conf   = v["conf_sum"] / v["total"]
                actual_acc = v["hits"] / v["total"]
                errors.append(abs(avg_conf - actual_acc))
        return round(sum(errors) / len(errors), 4) if errors else None

    adj_cal_error = calibration_error(all_props, "confidence")
    raw_cal_error = calibration_error(all_props, "confidence_raw")

    def acc(d):
        return round(d["hits"] / d["total"], 4) if d["total"] else None

    return {
        "total_props":     len(all_props),
        "total_hits":      sum(1 for p in all_props if p.get("hit")),
        "overall_accuracy":round(sum(1 for p in all_props if p.get("hit")) / len(all_props), 4),
        "adj_calibration_error": adj_cal_error,
        "raw_calibration_error": raw_cal_error,
        "adj_beats_raw":   adj_cal_error is not None and raw_cal_error is not None and adj_cal_error < raw_cal_error,
        "by_confidence_adj": bucket_accuracy(all_props, "confidence"),
        "by_confidence_raw": bucket_accuracy(all_props, "confidence_raw"),
        "by_sport":  {k: {"hits": v["hits"], "total": v["total"], "accuracy": acc(v)} for k, v in sport_acc.items()},
        "by_stat":   {k: {"hits": v["hits"], "total": v["total"], "accuracy": acc(v)} for k, v in sorted(stat_acc.items())},
        "by_tier":   {k: {"hits": v["hits"], "total": v["total"], "accuracy": acc(v)} for k, v in tier_acc.items()},
    }


# ============================================================
#  PARAMETER TUNING
# ============================================================

def tune_parameters(analysis: Dict, current_params: Dict, sessions_used: int) -> Dict:
    """
    Adjust model parameters based on observed accuracy.

    Rules:
    - If adjusted confidence calibration error < raw: keep/strengthen prior_weight
    - If raw beats adjusted: loosen prior_weight (reduce shrinkage)
    - If high-confidence props (80%+) are hitting at < 65%: reduce avg_factor_max
    - If high-confidence props are hitting at > 85%: slightly increase avg_factor_max
    - Changes are conservative — max 10% adjustment per training run
    - Parameters are bounded to reasonable ranges
    """
    params = dict(current_params)
    notes  = []

    if not analysis:
        params["notes"] = "Insufficient data for tuning."
        return params

    # Calibration comparison
    adj_err = analysis.get("adj_calibration_error")
    raw_err = analysis.get("raw_calibration_error")

    if adj_err is not None and raw_err is not None:
        if adj_err < raw_err:
            # Adjusted beats raw — shrinkage is helping, keep or strengthen slightly
            delta = min((raw_err - adj_err) * 5, 1.0)
            params["prior_weight"] = round(min(params["prior_weight"] + delta, 20.0), 2)
            notes.append(f"Adj conf beats raw (err {adj_err:.3f} < {raw_err:.3f}) — increased prior_weight to {params['prior_weight']}")
        else:
            # Raw beats adjusted — shrinkage may be too aggressive
            delta = min((adj_err - raw_err) * 5, 1.0)
            params["prior_weight"] = round(max(params["prior_weight"] - delta, 3.0), 2)
            notes.append(f"Raw conf beats adj (err {raw_err:.3f} < {adj_err:.3f}) — decreased prior_weight to {params['prior_weight']}")

    # High-confidence bucket accuracy
    high_conf = analysis.get("by_confidence_adj", {}).get("80-100%")
    if high_conf and high_conf["total"] >= 10:
        acc = high_conf["accuracy"]
        if acc < 0.65:
            params["avg_factor_max"] = round(max(params["avg_factor_max"] * 0.95, 1.10), 3)
            notes.append(f"High-conf accuracy {acc:.1%} < 65% — reduced avg_factor_max to {params['avg_factor_max']}")
        elif acc > 0.85:
            params["avg_factor_max"] = round(min(params["avg_factor_max"] * 1.05, 2.00), 3)
            notes.append(f"High-conf accuracy {acc:.1%} > 85% — increased avg_factor_max to {params['avg_factor_max']}")

    # Low-confidence bucket — if <50% bucket is hitting well, floor is too aggressive
    low_conf = analysis.get("by_confidence_adj", {}).get("<50%")
    if low_conf and low_conf["total"] >= 10:
        acc = low_conf["accuracy"]
        if acc > 0.55:
            params["avg_factor_min"] = round(max(params["avg_factor_min"] * 0.95, 0.50), 3)
            notes.append(f"Low-conf accuracy {acc:.1%} > 55% — reduced avg_factor_min to {params['avg_factor_min']}")

    params["version"]       = current_params.get("version", 1) + 1
    params["trained_on"]    = datetime.now().isoformat()
    params["sessions_used"] = sessions_used
    params["notes"]         = " | ".join(notes) if notes else "No parameter changes needed."

    return params


# ============================================================
#  WRITE OUTPUTS
# ============================================================

def write_model_params(root: Path, params: Dict) -> None:
    path = root / "model_params.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(params, fh, indent=2)
    print(f"  model_params.json saved (v{params['version']})", file=sys.stderr)
    print(f"  Notes: {params['notes']}", file=sys.stderr)


def write_model_performance(root: Path, sessions: List[Dict], analysis: Dict) -> None:
    """Write full performance history for the dashboard."""
    session_summaries = [
        {
            "folder":   s["folder"],
            "sport":    s["sport"],
            "date":     s["date"],
            "accuracy": s["accuracy"],
            "resolved": s["resolved"],
            "total":    s["total"],
        }
        for s in sessions
    ]

    payload = {
        "generated_at":   datetime.now().isoformat(),
        "sessions":       session_summaries,
        "analysis":       analysis,
    }

    path = root / "model_performance.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"  model_performance.json saved ({len(sessions)} sessions)", file=sys.stderr)


def load_current_params(root: Path) -> Dict:
    path = root / "model_params.json"
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    return dict(DEFAULT_PARAMS)


# ============================================================
#  CLI
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train model parameters from resolved prediction folders.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--output-root",
        default=".",
        help="Root directory containing dated folders and model files (default: current dir).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Analyze and print results without writing any files.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = Path(args.output_root)

    print("\nScanning for resolved prediction folders...", file=sys.stderr)
    folders = find_resolved_folders(root)

    if not folders:
        print("  No resolved folders found.", file=sys.stderr)
        print("  Run resolve_results.py first to generate actual_results.json files.", file=sys.stderr)
        return

    print(f"  Found {len(folders)} resolved session(s).", file=sys.stderr)

    # Load all sessions
    sessions = []
    for folder in folders:
        try:
            session = load_session(folder)
            if session:
                sessions.append(session)
                print(f"  Loaded {folder.name}: {len(session['props'])} matched props", file=sys.stderr)
        except Exception as e:
            print(f"  [WARN] Could not load {folder.name}: {e}", file=sys.stderr)

    if not sessions:
        print("  No usable session data found.", file=sys.stderr)
        return

    # Analyze
    print(f"\nAnalyzing {sum(len(s['props']) for s in sessions)} total props...", file=sys.stderr)
    analysis = analyze_sessions(sessions)

    print(f"\n  Overall accuracy:     {analysis.get('overall_accuracy', 0)*100:.1f}%", file=sys.stderr)
    print(f"  Total props resolved: {analysis.get('total_props', 0)}", file=sys.stderr)
    print(f"  Adj calibration err:  {analysis.get('adj_calibration_error')}", file=sys.stderr)
    print(f"  Raw calibration err:  {analysis.get('raw_calibration_error')}", file=sys.stderr)
    print(f"  Adj beats raw:        {analysis.get('adj_beats_raw')}", file=sys.stderr)

    # Tune parameters
    current_params = load_current_params(root)
    new_params     = tune_parameters(analysis, current_params, len(sessions))

    print(f"\n  Parameter changes:", file=sys.stderr)
    for k in ("prior_weight", "avg_factor_min", "avg_factor_max"):
        old = current_params.get(k)
        new = new_params.get(k)
        marker = " ← updated" if old != new else ""
        print(f"    {k}: {old} → {new}{marker}", file=sys.stderr)

    if args.dry_run:
        print("\n  [DRY RUN] No files written.", file=sys.stderr)
        return

    write_model_params(root, new_params)
    write_model_performance(root, sessions, analysis)
    print("\nDone. Run viz_props.py to view the performance dashboard.", file=sys.stderr)


if __name__ == "__main__":
    main()
