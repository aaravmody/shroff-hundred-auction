"""Central configuration for The Hundred fantasy system.

All file paths and tunable knobs live here so the pipeline scripts and the
Streamlit app agree on names and locations.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).parent

# ----------------------------------------------------------------------------
# Roster / team configuration
# ----------------------------------------------------------------------------
# The league runs with 5 teams. Team codes come from the draft sheet
# (hundered.xlsx). Optionally give them nicer display names in TEAM_NAMES.
TEAMS = ["sd", "f9", "ssw", "cc", "lg"]

TEAM_NAMES = {
    "lg": "Lulli Gang",
    "sd": "Shroff Demolishers",
    "f9": "Falling 99 Supergiants",
    "ssw": "Slow Over Rate Super Wizards",
    "cc": "Capital C",
}

# Consistent colour per team, used across every chart in the dashboard.
TEAM_COLORS = {
    "ssw": "#22d3ee",  # cyan
    "sd": "#f472b6",   # pink
    "f9": "#a78bfa",   # violet
    "lg": "#4ade80",   # green
    "cc": "#fbbf24",   # amber
}

SQUAD_SIZE = 16  # players per team (per the draft sheet)
NUM_TEAMS = len(TEAMS)

VALID_ROLES = {"BAT", "BOWL", "AR", "WK", ""}

# ----------------------------------------------------------------------------
# Files
# ----------------------------------------------------------------------------
PLAYERS_FILE = BASE_DIR / "players.csv"

# Output of fetch_hundred_stats.py: per-player base fantasy points from the API.
PLAYER_STATS_FILE = BASE_DIR / "hundred_player_stats.xlsx"

# Output of fantasy_points.py: rosters scored + leaderboard for the dashboard.
FANTASY_WORKBOOK = BASE_DIR / "Hundred_Fantasy_Points.xlsx"

# History snapshots (appended each successful update cycle).
LEADERBOARD_HISTORY_FILE = BASE_DIR / "leaderboard_history.csv"
PLAYER_HISTORY_FILE = BASE_DIR / "player_points_history.csv"

STATUS_FILE = BASE_DIR / "update_status.json"
LOG_FILE = BASE_DIR / "auto_update.log"

# Raw API responses are cached here to conserve the (rate-limited) API quota.
API_CACHE_DIR = BASE_DIR / "api_cache"

# ----------------------------------------------------------------------------
# CricketData (cricketdata.org / cricapi.com) API
# ----------------------------------------------------------------------------
CRICKET_DATA_API_KEY = os.environ.get("CRICKET_DATA_API_KEY", "").strip()
CRICKET_DATA_BASE_URL = "https://api.cricapi.com/v1"

# Text used to auto-discover the competition in the series list. Defaults to
# the men's competition (the draft is a men's squad); discovery then picks the
# most recent edition whose window has started.
SERIES_SEARCH_TERM = os.environ.get("HUNDRED_SERIES_SEARCH", "The Hundred Men").strip()

# Optional: pin a known series id (skips auto-discovery when set). Discovery
# writes the resolved id to hundred_series.json so it is remembered between runs.
HUNDRED_SERIES_ID = os.environ.get("HUNDRED_SERIES_ID", "").strip()
RESOLVED_SERIES_FILE = BASE_DIR / "hundred_series.json"
