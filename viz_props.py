"""
viz_props.py

Local Flask dashboard for visualizing PrizePicks prop confidence results.

Reads confidence_results.json from the most recent dated folder and serves
an interactive dashboard at http://localhost:5000

Features:
  - Full prop table sortable by confidence, player, stat type, line
  - Filter by stat type, tier (standard/goblin/demon), confidence threshold
  - Color-coded confidence rows (green / yellow / red)
  - Slate builder: select 2-6 picks, shows combined probability per slate
  - Slates ranked by combined probability

Requirements:
  pip install flask

Usage
-----
python viz_props.py
python viz_props.py --sport mlb
python viz_props.py --folder ./04282026-MLB
python viz_props.py --port 8080
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

# Global state — loaded once at startup
RESULTS:    list  = []
META:       dict  = {}
DATA_FOLDER: Path = Path(".")


# ============================================================
#  FOLDER / FILE RESOLUTION  (mirrors analyze_props_confidence.py)
# ============================================================

def find_latest_folder(root: Path, sport: str = "MLB") -> tuple:
    all_dated = sorted(
        [p for p in root.iterdir() if p.is_dir() and re.match(r"^\d{8}-MLB$", p.name)],
        reverse=True,
    )
    if not all_dated:
        raise FileNotFoundError(
            f"No MMDDYYYY-MLB folder found under {root}. Run prizepicks_export.py first."
        )
    return all_dated[0], "mlb"


def load_results(folder: Path) -> tuple[list, dict]:
    json_path = folder / "confidence_results.json"
    if not json_path.exists():
        raise FileNotFoundError(
            f"confidence_results.json not found in {folder}. "
            "Run analyze_props_confidence.py first."
        )
    with json_path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("results", []), {
        "sport":        data.get("sport", ""),
        "season":       data.get("season", ""),
        "total_props":  data.get("total_props", 0),
        "generated_at": data.get("generated_at", ""),
    }


# ============================================================
#  SLATE BUILDER
# ============================================================

def build_slates(
    candidates: list,
    picks_per_slate: int,
    num_slates: int = 10,
) -> list:
    """
    Build slates of picks_per_slate props each.
    - No duplicate players within a slate
    - Game-diverse first pass, then backfill
    - No prop reused across slates
    - Ranked by combined probability (product of confidence scores)
    """
    from math import prod

    pool  = sorted(candidates, key=lambda x: x["confidence"], reverse=True)
    used  = set()
    slates = []

    for bet_num in range(1, num_slates + 1):
        picks:           list = []
        players_in_slip: set  = set()
        games_in_slip:   set  = set()

        for diverse_pass in (True, False):
            if len(picks) >= picks_per_slate:
                break
            for idx, prop in enumerate(pool):
                if len(picks) >= picks_per_slate:
                    break
                if idx in used:
                    continue
                if prop["player"] in players_in_slip:
                    continue
                if diverse_pass and prop.get("game_key", "") in games_in_slip:
                    continue
                picks.append(prop)
                used.add(idx)
                players_in_slip.add(prop["player"])
                games_in_slip.add(prop.get("game_key", ""))

        if not picks:
            break

        combined_prob = round(prod(p["confidence"] for p in picks), 4)
        slates.append({
            "bet":              bet_num,
            "picks":            picks,
            "combined_prob":    combined_prob,
            "combined_prob_pct": f"{combined_prob * 100:.1f}%",
            "picks_count":      len(picks),
        })

    slates.sort(key=lambda s: s["combined_prob"], reverse=True)
    for i, s in enumerate(slates, 1):
        s["rank"] = i

    return slates


# ============================================================
#  PERFORMANCE DATA
# ============================================================

PERFORMANCE: dict = {}
MODEL_PARAMS: dict = {}

def load_performance(folder: Path) -> None:
    global PERFORMANCE, MODEL_PARAMS
    root = folder.parent

    perf_path   = root / "model_performance.json"
    params_path = root / "model_params.json"

    if perf_path.exists():
        with perf_path.open("r", encoding="utf-8") as fh:
            PERFORMANCE = json.load(fh)

    if params_path.exists():
        with params_path.open("r", encoding="utf-8") as fh:
            MODEL_PARAMS = json.load(fh)


# ============================================================
#  ROUTES
# ============================================================

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE, meta=META)


@app.route("/performance")
def performance():
    return render_template_string(PERF_TEMPLATE, meta=META)


@app.route("/api/performance")
def api_performance():
    return jsonify({
        "performance":   PERFORMANCE,
        "model_params":  MODEL_PARAMS,
    })


@app.route("/api/props")
def api_props():
    """Return filtered + sorted props as JSON."""
    min_conf   = float(request.args.get("min_conf",   0))
    stat_type  = request.args.get("stat_type",  "all")
    odds_type  = request.args.get("odds_type",  "all")
    sort_by    = request.args.get("sort_by",    "confidence")
    sort_dir   = request.args.get("sort_dir",   "desc")
    player_q   = request.args.get("player",     "").lower()

    filtered = [
        r for r in RESULTS
        if r["confidence"] >= min_conf
        and (stat_type == "all" or r["stat_type"] == stat_type)
        and (odds_type == "all" or r["odds_type"] == odds_type)
        and (not player_q or player_q in r["player"].lower())
    ]

    reverse = sort_dir == "desc"
    filtered.sort(key=lambda x: x.get(sort_by, 0), reverse=reverse)

    # Build filter options from full dataset
    stat_types = sorted(set(r["stat_type"] for r in RESULTS))
    odds_types = sorted(set(r["odds_type"] for r in RESULTS))

    return jsonify({
        "props":      filtered,
        "total":      len(filtered),
        "stat_types": stat_types,
        "odds_types": odds_types,
        "meta":       META,
    })


@app.route("/api/slates")
def api_slates():
    """Build and return slates for given pick count."""
    picks_per_slate = int(request.args.get("picks", 4))
    picks_per_slate = max(2, min(6, picks_per_slate))

    min_conf  = float(request.args.get("min_conf",  0.50))
    stat_type = request.args.get("stat_type", "all")
    odds_type = request.args.get("odds_type", "all")

    candidates = [
        r for r in RESULTS
        if r["confidence"] >= min_conf
        and (stat_type == "all" or r["stat_type"] == stat_type)
        and (odds_type == "all" or r["odds_type"] == odds_type)
    ]

    if not candidates:
        return jsonify({"slates": [], "message": "No candidates meet the current filters."})

    slates = build_slates(candidates, picks_per_slate)
    return jsonify({"slates": slates, "picks_per_slate": picks_per_slate})


# ============================================================
#  HTML TEMPLATE
# ============================================================

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PrizePicks Prop Analyzer</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', sans-serif; background: #0f1117; color: #e2e8f0; min-height: 100vh; }

  header {
    background: linear-gradient(135deg, #1a1f2e, #252d3d);
    padding: 20px 32px;
    border-bottom: 1px solid #2d3748;
    display: flex; align-items: center; justify-content: space-between;
  }
  header h1 { font-size: 1.4rem; font-weight: 700; color: #63b3ed; letter-spacing: 0.5px; }
  header .meta { font-size: 0.78rem; color: #718096; }

  .container { padding: 24px 32px; }

  /* Tabs */
  .tabs { display: flex; gap: 4px; margin-bottom: 24px; border-bottom: 1px solid #2d3748; }
  .tab-btn {
    padding: 10px 20px; background: transparent; border: none; color: #718096;
    font-size: 0.88rem; cursor: pointer; border-bottom: 2px solid transparent;
    transition: all 0.2s;
  }
  .tab-btn.active { color: #63b3ed; border-bottom-color: #63b3ed; }
  .tab-btn:hover:not(.active) { color: #a0aec0; }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }

  /* Filters */
  .filters {
    background: #1a1f2e; border: 1px solid #2d3748; border-radius: 8px;
    padding: 16px 20px; margin-bottom: 20px;
    display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-end;
  }
  .filter-group { display: flex; flex-direction: column; gap: 5px; }
  .filter-group label { font-size: 0.72rem; color: #718096; text-transform: uppercase; letter-spacing: 0.5px; }
  .filter-group input, .filter-group select {
    background: #252d3d; border: 1px solid #2d3748; border-radius: 5px;
    color: #e2e8f0; padding: 6px 10px; font-size: 0.84rem; min-width: 140px;
  }
  .filter-group input:focus, .filter-group select:focus {
    outline: none; border-color: #63b3ed;
  }
  .btn {
    padding: 7px 16px; border-radius: 5px; border: none; cursor: pointer;
    font-size: 0.84rem; font-weight: 600; transition: all 0.2s;
  }
  .btn-primary { background: #3182ce; color: white; }
  .btn-primary:hover { background: #2b6cb0; }
  .btn-secondary { background: #2d3748; color: #a0aec0; }
  .btn-secondary:hover { background: #4a5568; color: #e2e8f0; }

  /* Stats bar */
  .stats-bar {
    display: flex; gap: 20px; margin-bottom: 16px;
    font-size: 0.82rem; color: #718096;
  }
  .stats-bar span { color: #a0aec0; }
  .stats-bar strong { color: #e2e8f0; }

  /* Table */
  .table-wrap { overflow-x: auto; border-radius: 8px; border: 1px solid #2d3748; }
  table { width: 100%; border-collapse: collapse; font-size: 0.84rem; }
  thead th {
    background: #1a1f2e; padding: 11px 14px; text-align: left;
    font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.5px;
    color: #718096; border-bottom: 1px solid #2d3748; cursor: pointer;
    user-select: none; white-space: nowrap;
  }
  thead th:hover { color: #a0aec0; }
  thead th .sort-arrow { margin-left: 4px; opacity: 0.4; }
  thead th.sorted .sort-arrow { opacity: 1; color: #63b3ed; }
  tbody tr { border-bottom: 1px solid #1a1f2e; transition: background 0.1s; }
  tbody tr:hover { background: #1a1f2e; }
  tbody td { padding: 10px 14px; }

  /* Confidence badges */
  .conf-badge {
    display: inline-block; padding: 2px 8px; border-radius: 12px;
    font-weight: 700; font-size: 0.8rem;
  }
  .conf-high  { background: #1c4532; color: #68d391; }
  .conf-mid   { background: #3d2c00; color: #f6ad55; }
  .conf-low   { background: #3d1515; color: #fc8181; }

  /* Tier badge */
  .tier-badge {
    display: inline-block; padding: 2px 7px; border-radius: 4px;
    font-size: 0.72rem; font-weight: 600; text-transform: uppercase;
  }
  .tier-standard { background: #2b4c7e; color: #90cdf4; }
  .tier-demon    { background: #44337a; color: #d6bcfa; }
  .tier-goblin   { background: #22543d; color: #9ae6b4; }

  /* Slate builder */
  .slate-controls {
    background: #1a1f2e; border: 1px solid #2d3748; border-radius: 8px;
    padding: 16px 20px; margin-bottom: 20px;
    display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-end;
  }
  .picks-selector { display: flex; gap: 6px; }
  .pick-btn {
    width: 38px; height: 38px; border-radius: 50%; border: 2px solid #2d3748;
    background: transparent; color: #718096; font-weight: 700; cursor: pointer;
    transition: all 0.2s; font-size: 0.9rem;
  }
  .pick-btn.active { border-color: #63b3ed; color: #63b3ed; background: #1a3150; }
  .pick-btn:hover:not(.active) { border-color: #4a5568; color: #a0aec0; }

  .slates-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; }
  .slate-card {
    background: #1a1f2e; border: 1px solid #2d3748; border-radius: 8px; padding: 16px;
  }
  .slate-card-header {
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 12px; padding-bottom: 10px; border-bottom: 1px solid #2d3748;
  }
  .slate-rank { font-size: 0.72rem; color: #718096; }
  .slate-prob {
    font-size: 1.1rem; font-weight: 700;
  }
  .slate-pick {
    display: flex; justify-content: space-between; align-items: center;
    padding: 7px 0; border-bottom: 1px solid #252d3d; font-size: 0.82rem;
  }
  .slate-pick:last-child { border-bottom: none; }
  .slate-pick-player { font-weight: 600; color: #e2e8f0; }
  .slate-pick-stat { color: #a0aec0; font-size: 0.78rem; }
  .slate-pick-line { color: #63b3ed; font-weight: 600; }
  .slate-pick-conf { font-size: 0.78rem; }

  .loading { text-align: center; padding: 40px; color: #718096; }
  .empty   { text-align: center; padding: 40px; color: #4a5568; font-style: italic; }

  .prob-high { color: #68d391; }
  .prob-mid  { color: #f6ad55; }
  .prob-low  { color: #fc8181; }
</style>
</head>
<body>

<header>
  <div>
    <h1>⚾ MLB PrizePicks Prop Analyzer</h1>
    <div class="meta" id="header-meta">Loading...</div>
  </div>
  <a href="/performance" style="color:#63b3ed;text-decoration:none;font-size:0.84rem;padding:6px 14px;border:1px solid #2d3748;border-radius:5px;">
    📊 Model Performance
  </a>
</header>

<div class="container">
  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('props')">All Props</button>
    <button class="tab-btn" onclick="switchTab('slates')">Slate Builder</button>
  </div>

  <!-- PROPS TAB -->
  <div id="tab-props" class="tab-panel active">
    <div class="filters">
      <div class="filter-group">
        <label>Player Search</label>
        <input type="text" id="f-player" placeholder="Search player..." oninput="loadProps()">
      </div>
      <div class="filter-group">
        <label>Stat Type</label>
        <select id="f-stat" onchange="loadProps()">
          <option value="all">All Stats</option>
        </select>
      </div>
      <div class="filter-group">
        <label>Tier</label>
        <select id="f-tier" onchange="loadProps()">
          <option value="all">All Tiers</option>
          <option value="standard">Standard</option>
          <option value="demon">Demon</option>
          <option value="goblin">Goblin</option>
        </select>
      </div>
      <div class="filter-group">
        <label>Min Confidence</label>
        <select id="f-conf" onchange="loadProps()">
          <option value="0">Any</option>
          <option value="0.5">50%+</option>
          <option value="0.6">60%+</option>
          <option value="0.7" selected>70%+</option>
          <option value="0.8">80%+</option>
          <option value="0.9">90%+</option>
        </select>
      </div>
      <div class="filter-group">
        <label>Sort By</label>
        <select id="f-sort" onchange="loadProps()">
          <option value="confidence">Adj. Confidence</option>
          <option value="confidence_raw">Raw Confidence</option>
          <option value="player">Player</option>
          <option value="line">Line</option>
          <option value="average">Season Avg</option>
          <option value="games">Games</option>
        </select>
      </div>
      <button class="btn btn-secondary" onclick="resetFilters()">Reset</button>
    </div>

    <div class="stats-bar" id="stats-bar"></div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th onclick="sortBy('player')">Player <span class="sort-arrow">↕</span></th>
            <th onclick="sortBy('stat_type')">Stat <span class="sort-arrow">↕</span></th>
            <th onclick="sortBy('line')">Line <span class="sort-arrow">↕</span></th>
            <th onclick="sortBy('odds_type')">Tier <span class="sort-arrow">↕</span></th>
            <th onclick="sortBy('confidence')">Adj. Conf <span class="sort-arrow">↕</span></th>
            <th onclick="sortBy('confidence_raw')">Raw Conf <span class="sort-arrow">↕</span></th>
            <th onclick="sortBy('average')">Season Avg <span class="sort-arrow">↕</span></th>
            <th onclick="sortBy('hits')">Hits/Games <span class="sort-arrow">↕</span></th>
            <th>Opponent</th>
          </tr>
        </thead>
        <tbody id="props-body">
          <tr><td colspan="8" class="loading">Loading props...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- SLATES TAB -->
  <div id="tab-slates" class="tab-panel">
    <div class="slate-controls">
      <div class="filter-group">
        <label>Picks Per Slate</label>
        <div class="picks-selector" id="picks-selector">
          <button class="pick-btn" onclick="setPicks(2)">2</button>
          <button class="pick-btn" onclick="setPicks(3)">3</button>
          <button class="pick-btn active" onclick="setPicks(4)">4</button>
          <button class="pick-btn" onclick="setPicks(5)">5</button>
          <button class="pick-btn" onclick="setPicks(6)">6</button>
        </div>
      </div>
      <div class="filter-group">
        <label>Min Confidence</label>
        <select id="s-conf" onchange="loadSlates()">
          <option value="0.5">50%+</option>
          <option value="0.6">60%+</option>
          <option value="0.7" selected>70%+</option>
          <option value="0.8">80%+</option>
          <option value="0.9">90%+</option>
        </select>
      </div>
      <div class="filter-group">
        <label>Tier</label>
        <select id="s-tier" onchange="loadSlates()">
          <option value="all">All Tiers</option>
          <option value="standard">Standard Only</option>
          <option value="demon">Demon Only</option>
          <option value="goblin">Goblin Only</option>
        </select>
      </div>
      <button class="btn btn-primary" onclick="loadSlates()">Build Slates</button>
    </div>

    <div class="stats-bar" id="slates-stats"></div>
    <div class="slates-grid" id="slates-grid">
      <div class="empty">Select your options above and click Build Slates.</div>
    </div>
  </div>
</div>

<script>
let currentSort = 'confidence';
let currentDir  = 'desc';
let currentPicks = 4;

// ── Init ──────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  loadProps();
});

function switchTab(tab) {
  document.querySelectorAll('.tab-btn').forEach((b, i) => {
    b.classList.toggle('active', ['props','slates'][i] === tab);
  });
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
}

// ── Props ─────────────────────────────────────────────────
async function loadProps() {
  const params = new URLSearchParams({
    min_conf:  document.getElementById('f-conf').value,
    stat_type: document.getElementById('f-stat').value,
    odds_type: document.getElementById('f-tier').value,
    player:    document.getElementById('f-player').value,
    sort_by:   currentSort,
    sort_dir:  currentDir,
  });

  const res  = await fetch('/api/props?' + params);
  const data = await res.json();

  // Populate filter dropdowns (first load)
  const statSel = document.getElementById('f-stat');
  if (statSel.options.length === 1) {
    data.stat_types.forEach(st => {
      const o = document.createElement('option');
      o.value = st; o.textContent = st;
      statSel.appendChild(o);
    });
  }

  // Header meta
  if (data.meta && data.meta.sport) {
    document.getElementById('header-meta').textContent =
      `${data.meta.sport} · Season ${data.meta.season} · Generated ${new Date(data.meta.generated_at).toLocaleString()}`;
  }

  // Stats bar
  document.getElementById('stats-bar').innerHTML =
    `Showing <strong>${data.total}</strong> props`;

  // Table
  const tbody = document.getElementById('props-body');
  if (!data.props.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty">No props match current filters.</td></tr>';
    return;
  }

  tbody.innerHTML = data.props.map(p => {
    const confClass    = p.confidence     >= 0.8 ? 'conf-high' : p.confidence     >= 0.6 ? 'conf-mid' : 'conf-low';
    const rawConfClass = p.confidence_raw >= 0.8 ? 'conf-high' : p.confidence_raw >= 0.6 ? 'conf-mid' : 'conf-low';
    const tierClass    = `tier-${p.odds_type}`;
    const sampleWarn   = p.games < 5 ? ' title="Small sample — use caution"' : '';
    return `<tr>
      <td><strong>${p.player}</strong><br><small style="color:#718096">${p.team || ''}</small></td>
      <td>${p.stat_type}</td>
      <td><strong>${p.line}</strong></td>
      <td><span class="tier-badge ${tierClass}">${p.odds_type}</span></td>
      <td><span class="conf-badge ${confClass}"${sampleWarn}>${(p.confidence * 100).toFixed(1)}%</span></td>
      <td><span class="conf-badge ${rawConfClass}" style="opacity:0.7"${sampleWarn}>${(p.confidence_raw * 100).toFixed(1)}%</span>${p.games < 5 ? ' <small style="color:#718096">⚠</small>' : ''}</td>
      <td>${p.average}</td>
      <td>${p.hits}/${p.games}</td>
      <td>${p.opponent || '—'}</td>
    </tr>`;
  }).join('');

  // Sort indicators
  document.querySelectorAll('thead th').forEach(th => th.classList.remove('sorted'));
}

function sortBy(col) {
  if (currentSort === col) {
    currentDir = currentDir === 'desc' ? 'asc' : 'desc';
  } else {
    currentSort = col;
    currentDir  = 'desc';
  }
  document.getElementById('f-sort').value = col;
  loadProps();
}

function resetFilters() {
  document.getElementById('f-player').value = '';
  document.getElementById('f-stat').value   = 'all';
  document.getElementById('f-tier').value   = 'all';
  document.getElementById('f-conf').value   = '0.7';
  document.getElementById('f-sort').value   = 'confidence';
  currentSort = 'confidence';
  currentDir  = 'desc';
  loadProps();
}

// ── Slates ────────────────────────────────────────────────
function setPicks(n) {
  currentPicks = n;
  document.querySelectorAll('.pick-btn').forEach(b => {
    b.classList.toggle('active', parseInt(b.textContent) === n);
  });
  loadSlates();
}

async function loadSlates() {
  const params = new URLSearchParams({
    picks:     currentPicks,
    min_conf:  document.getElementById('s-conf').value,
    odds_type: document.getElementById('s-tier').value,
  });

  const res  = await fetch('/api/slates?' + params);
  const data = await res.json();

  const grid = document.getElementById('slates-grid');

  if (!data.slates || !data.slates.length) {
    grid.innerHTML = `<div class="empty">${data.message || 'No slates could be built with current filters.'}</div>`;
    document.getElementById('slates-stats').innerHTML = '';
    return;
  }

  document.getElementById('slates-stats').innerHTML =
    `<strong>${data.slates.length}</strong> slates built · <strong>${data.picks_per_slate}</strong> picks each · ranked by combined probability`;

  grid.innerHTML = data.slates.map(s => {
    const probClass = s.combined_prob >= 0.3 ? 'prob-high' : s.combined_prob >= 0.15 ? 'prob-mid' : 'prob-low';
    const picks = s.picks.map(p => {
      const confClass = p.confidence >= 0.8 ? 'conf-high' : p.confidence >= 0.6 ? 'conf-mid' : 'conf-low';
      return `<div class="slate-pick">
        <div>
          <div class="slate-pick-player">${p.player}</div>
          <div class="slate-pick-stat">${p.stat_type} OVER ${p.line}</div>
        </div>
        <div style="text-align:right">
          <span class="conf-badge ${confClass} slate-pick-conf">${(p.confidence * 100).toFixed(1)}%</span>
          <div style="font-size:0.72rem;color:#718096;margin-top:2px">avg ${p.average}</div>
        </div>
      </div>`;
    }).join('');

    return `<div class="slate-card">
      <div class="slate-card-header">
        <span class="slate-rank">Slate #${s.rank}</span>
        <span class="slate-prob ${probClass}">${s.combined_prob_pct} combined</span>
      </div>
      ${picks}
    </div>`;
  }).join('');
}
</script>
</body>
</html>
"""


# ============================================================
#  PERFORMANCE TEMPLATE
# ============================================================

PERF_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Model Performance — MLB Prop Analyzer</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', sans-serif; background: #0f1117; color: #e2e8f0; min-height: 100vh; }
  header {
    background: linear-gradient(135deg, #1a1f2e, #252d3d);
    padding: 20px 32px; border-bottom: 1px solid #2d3748;
    display: flex; align-items: center; justify-content: space-between;
  }
  header h1 { font-size: 1.4rem; font-weight: 700; color: #63b3ed; }
  header .meta { font-size: 0.78rem; color: #718096; }
  .nav-link {
    color: #63b3ed; text-decoration: none; font-size: 0.84rem;
    padding: 6px 14px; border: 1px solid #2d3748; border-radius: 5px;
  }
  .nav-link:hover { background: #1a1f2e; }
  .container { padding: 24px 32px; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }
  .grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 20px; margin-bottom: 20px; }
  .card {
    background: #1a1f2e; border: 1px solid #2d3748; border-radius: 8px; padding: 20px;
  }
  .card h3 { font-size: 0.82rem; text-transform: uppercase; letter-spacing: 0.5px; color: #718096; margin-bottom: 14px; }
  .big-stat { font-size: 2.4rem; font-weight: 700; color: #63b3ed; }
  .big-stat-label { font-size: 0.78rem; color: #718096; margin-top: 4px; }
  .table-wrap { overflow-x: auto; border-radius: 8px; border: 1px solid #2d3748; margin-bottom: 20px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.84rem; }
  thead th {
    background: #1a1f2e; padding: 10px 14px; text-align: left;
    font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.5px;
    color: #718096; border-bottom: 1px solid #2d3748;
  }
  tbody tr { border-bottom: 1px solid #1a1f2e; }
  tbody tr:hover { background: #1a1f2e; }
  tbody td { padding: 9px 14px; }
  .acc-high { color: #68d391; font-weight: 700; }
  .acc-mid  { color: #f6ad55; font-weight: 700; }
  .acc-low  { color: #fc8181; font-weight: 700; }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 0.72rem; font-weight: 600;
  }
  .badge-green { background: #1c4532; color: #68d391; }
  .badge-blue  { background: #1a365d; color: #90cdf4; }
  .badge-purple{ background: #44337a; color: #d6bcfa; }
  .param-row { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #252d3d; font-size: 0.84rem; }
  .param-row:last-child { border-bottom: none; }
  .param-label { color: #a0aec0; }
  .param-value { color: #63b3ed; font-weight: 600; font-family: monospace; }
  .calibration-bar { display: flex; align-items: center; gap: 10px; margin: 8px 0; }
  .calibration-bar .label { width: 80px; font-size: 0.78rem; color: #a0aec0; }
  .bar-track { flex: 1; height: 8px; background: #252d3d; border-radius: 4px; overflow: hidden; }
  .bar-fill  { height: 100%; border-radius: 4px; transition: width 0.5s; }
  .bar-adj   { background: #63b3ed; }
  .bar-raw   { background: #9f7aea; }
  .section-title { font-size: 1rem; font-weight: 700; color: #e2e8f0; margin: 24px 0 12px; }
  .empty { text-align: center; padding: 40px; color: #4a5568; font-style: italic; }
  .winner-badge { font-size: 0.7rem; padding: 1px 6px; border-radius: 3px; margin-left: 6px; }
</style>
</head>
<body>
<header>
  <div>
    <h1>📊 Model Performance</h1>
    <div class="meta" id="header-meta">Loading...</div>
  </div>
  <a href="/" class="nav-link">← Back to Props</a>
</header>

<div class="container">
  <div id="content"><div class="empty">Loading performance data...</div></div>
</div>

<script>
async function loadPerf() {
  const res  = await fetch('/api/performance');
  const data = await res.json();
  const perf = data.performance || {};
  const params = data.model_params || {};
  const analysis = perf.analysis || {};
  const sessions = perf.sessions || [];

  if (!sessions.length && !analysis.total_props) {
    document.getElementById('content').innerHTML =
      '<div class="empty">No performance data yet.<br>Run resolve_results.py and train_model.py after games finish.</div>';
    return;
  }

  document.getElementById('header-meta').textContent =
    `Generated ${perf.generated_at ? new Date(perf.generated_at).toLocaleString() : 'unknown'} · ${sessions.length} sessions`;

  const accClass = a => a >= 0.70 ? 'acc-high' : a >= 0.55 ? 'acc-mid' : 'acc-low';
  const pct      = a => a != null ? (a * 100).toFixed(1) + '%' : '—';

  // Summary cards
  const adjErr  = analysis.adj_calibration_error;
  const rawErr  = analysis.raw_calibration_error;
  const adjWins = analysis.adj_beats_raw;

  let html = `
  <div class="grid-3">
    <div class="card">
      <h3>Overall Accuracy</h3>
      <div class="big-stat ${accClass(analysis.overall_accuracy || 0)}">${pct(analysis.overall_accuracy)}</div>
      <div class="big-stat-label">${analysis.total_hits || 0} hits / ${analysis.total_props || 0} props</div>
    </div>
    <div class="card">
      <h3>Sessions Analyzed</h3>
      <div class="big-stat">${sessions.length}</div>
      <div class="big-stat-label">resolved prediction folders</div>
    </div>
    <div class="card">
      <h3>Model Version</h3>
      <div class="big-stat" style="color:#9f7aea">v${params.version || 1}</div>
      <div class="big-stat-label">${params.sessions_used || 0} sessions used for training</div>
    </div>
  </div>

  <div class="grid-2">
    <div class="card">
      <h3>Calibration — Adjusted vs Raw Confidence
        ${adjWins
          ? '<span class="badge badge-blue winner-badge">ADJ WINS</span>'
          : '<span class="badge badge-purple winner-badge">RAW WINS</span>'}
      </h3>
      <p style="font-size:0.78rem;color:#718096;margin-bottom:12px">
        Lower calibration error = confidence score better matches actual hit rate.
      </p>
      <div class="calibration-bar">
        <span class="label">Adjusted</span>
        <div class="bar-track"><div class="bar-fill bar-adj" style="width:${adjErr != null ? Math.min(adjErr*400,100) : 0}%"></div></div>
        <span style="font-size:0.82rem;color:#63b3ed;width:50px">${adjErr != null ? adjErr.toFixed(3) : '—'}</span>
      </div>
      <div class="calibration-bar">
        <span class="label">Raw</span>
        <div class="bar-track"><div class="bar-fill bar-raw" style="width:${rawErr != null ? Math.min(rawErr*400,100) : 0}%"></div></div>
        <span style="font-size:0.82rem;color:#9f7aea;width:50px">${rawErr != null ? rawErr.toFixed(3) : '—'}</span>
      </div>
    </div>

    <div class="card">
      <h3>Current Model Parameters</h3>
      <div class="param-row"><span class="param-label">Prior Weight (shrinkage)</span><span class="param-value">${params.prior_weight ?? '—'}</span></div>
      <div class="param-row"><span class="param-label">Avg Factor Min</span><span class="param-value">${params.avg_factor_min ?? '—'}</span></div>
      <div class="param-row"><span class="param-label">Avg Factor Max</span><span class="param-value">${params.avg_factor_max ?? '—'}</span></div>
      <div class="param-row"><span class="param-label">Min Games Warning</span><span class="param-value">${params.min_games_warn ?? '—'}</span></div>
      <div style="margin-top:10px;font-size:0.76rem;color:#718096;font-style:italic">${params.notes || ''}</div>
    </div>
  </div>

  <div class="grid-2">
    <div class="card">
      <h3>Accuracy by Confidence Bucket (Adjusted)</h3>
      <table><thead><tr><th>Bucket</th><th>Hits</th><th>Total</th><th>Accuracy</th></tr></thead><tbody>`;

  const byConf = analysis.by_confidence_adj || {};
  for (const [bucket, v] of Object.entries(byConf)) {
    html += `<tr><td>${bucket}</td><td>${v.hits}</td><td>${v.total}</td>
      <td class="${accClass(v.accuracy || 0)}">${pct(v.accuracy)}</td></tr>`;
  }
  html += `</tbody></table></div>

    <div class="card">
      <h3>Accuracy by Confidence Bucket (Raw)</h3>
      <table><thead><tr><th>Bucket</th><th>Hits</th><th>Total</th><th>Accuracy</th></tr></thead><tbody>`;

  const byConfRaw = analysis.by_confidence_raw || {};
  for (const [bucket, v] of Object.entries(byConfRaw)) {
    html += `<tr><td>${bucket}</td><td>${v.hits}</td><td>${v.total}</td>
      <td class="${accClass(v.accuracy || 0)}">${pct(v.accuracy)}</td></tr>`;
  }
  html += `</tbody></table></div></div>`;

  // By stat type
  html += `<div class="section-title">Accuracy by Stat Type</div>
  <div class="table-wrap"><table>
    <thead><tr><th>Stat Type</th><th>Hits</th><th>Total</th><th>Accuracy</th></tr></thead><tbody>`;
  const byStat = analysis.by_stat || {};
  for (const [stat, v] of Object.entries(byStat).sort((a,b) => (b[1].accuracy||0)-(a[1].accuracy||0))) {
    html += `<tr><td>${stat}</td><td>${v.hits}</td><td>${v.total}</td>
      <td class="${accClass(v.accuracy || 0)}">${pct(v.accuracy)}</td></tr>`;
  }
  html += `</tbody></table></div>`;

  // Session history
  html += `<div class="section-title">Session History</div>
  <div class="table-wrap"><table>
    <thead><tr><th>Folder</th><th>Sport</th><th>Resolved</th><th>Total</th><th>Accuracy</th></tr></thead><tbody>`;
  for (const s of [...sessions].reverse()) {
    html += `<tr>
      <td style="font-family:monospace;font-size:0.8rem">${s.folder}</td>
      <td>${s.sport}</td><td>${s.resolved}</td><td>${s.total}</td>
      <td class="${accClass(s.accuracy || 0)}">${pct(s.accuracy)}</td></tr>`;
  }
  html += `</tbody></table></div>`;

  document.getElementById('content').innerHTML = html;
}

loadPerf();
</script>
</body>
</html>
"""


# ============================================================
#  CLI
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Local Flask dashboard for PrizePicks prop confidence results.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Sport is fixed to MLB
    parser.add_argument(
        "--folder",
        default=None,
        help="Explicit path to dated folder containing confidence_results.json.",
    )
    parser.add_argument(
        "--output-root",
        default=".",
        help="Root directory to search for dated folders (default: current dir).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="Port to run the Flask server on (default: 5000).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Run Flask in debug mode.",
    )
    return parser.parse_args()


def main() -> None:
    global RESULTS, META, DATA_FOLDER

    args = parse_args()

    if args.folder:
        DATA_FOLDER = Path(args.folder)
    if args.folder:
        DATA_FOLDER = Path(args.folder)
    else:
        DATA_FOLDER, _ = find_latest_folder(Path(args.output_root))
    print(f"Loading results from: {DATA_FOLDER}")
    RESULTS, META = load_results(DATA_FOLDER)
    load_performance(DATA_FOLDER)
    print(f"Loaded {len(RESULTS)} props for {META.get('sport', '')} {META.get('season', '')}")
    print(f"\nDashboard running at http://localhost:{args.port}\n")

    app.run(host="0.0.0.0", port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
