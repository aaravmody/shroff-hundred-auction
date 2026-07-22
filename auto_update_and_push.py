"""Orchestrate one update cycle for The Hundred fantasy system.

Steps: git sync -> fetch stats from CricketData -> score the fantasy squads ->
snapshot history -> commit & push any changes. Designed to be run on a schedule
(GitHub Actions) after each match day.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import config

BASE_DIR = config.BASE_DIR
PYTHON = sys.executable  # use the interpreter running this script


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(cmd: list[str]) -> bool:
    print("\nRunning:", " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, cwd=BASE_DIR).returncode == 0


def has_staged_changes() -> bool:
    return subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=BASE_DIR).returncode != 0


def load_status() -> dict:
    if config.STATUS_FILE.exists():
        try:
            return json.loads(config.STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_status(update: dict) -> None:
    current = load_status()
    current.update(update)
    config.STATUS_FILE.write_text(json.dumps(current, indent=2), encoding="utf-8")


def check_server_status() -> str:
    try:
        socket.gethostname()
        return "running"
    except Exception:
        return "issue"


def append_csv_snapshot(df: pd.DataFrame, path: Path) -> None:
    if df.empty:
        return
    out = df.copy()
    out["snapshot_time"] = now_iso()
    if path.exists():
        out.to_csv(path, mode="a", header=False, index=False)
    else:
        out.to_csv(path, index=False)


def update_history_files() -> bool:
    if not config.FANTASY_WORKBOOK.exists():
        print("Fantasy workbook not found. Cannot update history.")
        return False
    try:
        leaderboard_df = pd.read_excel(config.FANTASY_WORKBOOK, sheet_name="Leaderboard")
        player_points_df = pd.read_excel(config.FANTASY_WORKBOOK, sheet_name="Player_Points")
    except Exception as exc:
        print(f"Failed to read workbook for history update: {exc}")
        return False
    append_csv_snapshot(leaderboard_df, config.LEADERBOARD_HISTORY_FILE)
    append_csv_snapshot(player_points_df, config.PLAYER_HISTORY_FILE)
    return True


def main() -> None:
    if not run(["git", "pull", "--rebase", "origin", "main"]):
        print("Git sync failed.")

    save_status(
        {
            "last_cycle_started": now_iso(),
            "server_status": check_server_status(),
            "data_source_status": "starting",
            "last_cycle_result": "running",
        }
    )

    ok_fetch = run([PYTHON, "fetch_hundred_stats.py"])
    if ok_fetch:
        save_status(
            {
                "last_successful_scrape_time": now_iso(),
                "data_source_status": "healthy",
            }
        )

    ok_points = run([PYTHON, "fantasy_points.py"]) if ok_fetch else False
    ok_history = update_history_files() if ok_fetch and ok_points else False

    if ok_fetch and ok_points and ok_history:
        save_status(
            {
                "last_cycle_finished": now_iso(),
                "last_cycle_result": "success",
                "server_status": check_server_status(),
                "data_source_status": "healthy",
            }
        )
        run(
            [
                "git", "add",
                config.PLAYER_STATS_FILE.name,
                config.FANTASY_WORKBOOK.name,
                config.LEADERBOARD_HISTORY_FILE.name,
                config.PLAYER_HISTORY_FILE.name,
                config.STATUS_FILE.name,
                config.RESOLVED_SERIES_FILE.name,
                "players.csv",
            ]
        )
        if has_staged_changes():
            if run(["git", "commit", "-m", "auto update fantasy data"]):
                save_status({"last_successful_git_push_time": now_iso()})
                run(["git", "add", config.STATUS_FILE.name])
                run(["git", "commit", "--amend", "--no-edit"])
                if run(["git", "push"]):
                    print("Updated files pushed to GitHub.")
                else:
                    print("Git push failed.")
        else:
            print("No changes detected. Nothing to push.")
    else:
        save_status(
            {
                "last_cycle_finished": now_iso(),
                "last_cycle_result": "update_failed",
                "server_status": check_server_status(),
                "data_source_status": "issue" if not ok_fetch else "healthy",
            }
        )
        run(["git", "add", config.STATUS_FILE.name])
        if has_staged_changes():
            run(["git", "commit", "-m", "update automation status"])
            run(["git", "push"])
        print("Update failed. Skipping data push.")


if __name__ == "__main__":
    main()
