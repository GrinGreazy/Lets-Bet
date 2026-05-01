# MLB PrizePicks Prop Confidence Analyzer

A self-improving pipeline for fetching MLB PrizePicks prop lines, scoring them against historical player performance using the MLB Stats API, visualizing results in an interactive dashboard, and learning from outcomes over time.

---

## Pipeline Overview

```
Day of games:
  prizepicks_export.py          → fetch MLB prop lines → MMDDYYYY-MLB/
  analyze_props_confidence.py   → score vs history    → confidence_results.json

  viz_props.py                  → interactive dashboard at localhost:5000

After games finish (24+ hrs):
  resolve_results.py            → fetch actual outcomes → actual_results.json
  train_model.py                → update model params   → model_params.json
```

Each resolved session improves the model. Parameters are tuned automatically based on how well predictions matched reality.

---

## Scripts

### 1. `prizepicks_export.py`
Fetches all available MLB prop lines from the PrizePicks partner API and saves them to a dated folder.

**Output:** `./MMDDYYYY-MLB/prizepicks_MLB.csv`

```bash
python prizepicks_export.py
```

---

### 2. `analyze_props_confidence.py`
Reads the most recent `MMDDYYYY-MLB` folder, fetches full season game logs for every player from the MLB Stats API, and scores each prop using Bayesian-adjusted confidence.

**Confidence scoring:**
- **Raw confidence** = % of games this season where the player exceeded the prop line
- **Adjusted confidence** = Bayesian shrinkage toward 50% for small samples, reinforced by season average vs line
- Both values stored and displayed side by side

If `model_params.json` exists, tuned parameters are loaded automatically at startup.

**No API key required** — uses the public MLB Stats API.

**Output:** `./MMDDYYYY-MLB/confidence_results.json`

```bash
python analyze_props_confidence.py
```

---

### 3. `viz_props.py`
Local Flask dashboard at `http://localhost:5000`.

**Props tab:**
- Full prop table sortable by adjusted confidence, raw confidence, player, stat type, line, season average
- Filter by stat type, tier (standard/demon/goblin), confidence threshold, player name
- Color-coded confidence (green 80%+, yellow 60–79%, red below 60%)
- ⚠ warning for props with fewer than 5 games of historical data

**Slate Builder tab:**
- Select 2–6 picks per slate
- Combined probability score per slate (product of individual confidence values)
- Slates ranked by combined probability

**Model Performance page** (`/performance`):
- Overall prediction accuracy across all resolved sessions
- Calibration comparison: adjusted vs raw confidence — shows which predicts better
- Accuracy broken down by confidence bucket, stat type, and tier
- Current model parameters with change notes
- Full session history

```bash
python viz_props.py
```

---

### 4. `resolve_results.py`
Scans all dated folders for unresolved predictions. For each folder where games have finished (default: 24 hours after earliest game start), fetches actual outcomes from the MLB Stats API and compares against predictions.

Tells you exactly how long until each pending folder is eligible for resolution.

**Output:** `./MMDDYYYY-MLB/actual_results.json`

```bash
python resolve_results.py            # auto-scan all unresolved folders
python resolve_results.py --hours 12 # custom wait threshold
```

---

### 5. `train_model.py`
Reads all resolved folders, calculates prediction accuracy, compares adjusted vs raw confidence calibration error, and updates model parameters accordingly. Also writes the performance history consumed by the dashboard.

```bash
python train_model.py           # analyze and update parameters
python train_model.py --dry-run # analyze without writing files
```

---

## Installation

```bash
pip install requests flask
```

---

## Folder Structure

```
./
├── 04282026-MLB/
│   ├── prizepicks_MLB.csv          ← prop lines (from prizepicks_export.py)
│   ├── confidence_results.json     ← predictions (from analyze_props_confidence.py)
│   └── actual_results.json         ← actual outcomes (from resolve_results.py)
├── model_params.json               ← tuned parameters (from train_model.py)
├── model_performance.json          ← historical accuracy (from train_model.py)
├── prizepicks_export.py
├── analyze_props_confidence.py
├── viz_props.py
├── resolve_results.py
├── train_model.py
└── README.md
```

---

## Key Concepts

| Term | Description |
|------|-------------|
| `confidence` | Bayesian-adjusted hit rate — primary score used for sorting and slates |
| `confidence_raw` | Plain historical hit rate — shown for reference |
| `prior_weight` | Shrinkage strength — higher = more conservative with small samples |
| `avg_factor` | Season average vs line multiplier — reinforces or dampens confidence |
| `combined_prob` | Product of all pick confidences in a slate — honest probability all legs hit |
| Calibration error | Mean difference between predicted confidence and actual accuracy — lower is better |

---

## Data Sources

| Data | Source | API Key Required |
|------|--------|-----------------|
| MLB prop lines | PrizePicks Partner API | No |
| MLB player game logs | MLB Stats API (statsapi.mlb.com) | No |
| Actual game outcomes | MLB Stats API (statsapi.mlb.com) | No |
