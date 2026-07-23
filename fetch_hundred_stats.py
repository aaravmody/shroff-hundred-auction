"""Fetch The Hundred stats from CricketData and compute base fantasy points.

Pipeline:
  1. Resolve the competition's series id (env override -> saved file -> auto
     search for "The Hundred").
  2. Pull the series match list, then a scorecard for every match that has
     started.
  3. Parse each scorecard into per-match, per-player performances (batting,
     bowling and fielding -- fielding is derived from the dismissal text).
  4. Score every performance with ``scoring`` and aggregate per player.
  5. Write ``hundred_player_stats.xlsx`` (base points + raw stat totals).

The output is *team-agnostic* -- captain/vice-captain multipliers and the
mapping onto the 5 fantasy squads happen later in ``fantasy_points.py``.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

import config
import scoring
from cricket_data_api import CricketDataClient, CricketDataError


# ---------------------------------------------------------------------------
# name handling
# ---------------------------------------------------------------------------
def clean_name(name: str) -> str:
    """Human-readable name: strip keeper dagger, sub markers, whitespace."""
    name = str(name or "").strip()
    name = name.replace("†", "").replace("†", "")  # keeper dagger
    name = re.sub(r"\bsub\b", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\(.*?\)", "", name)  # drop "(c)", "(wk)" etc.
    name = re.sub(r"\s+", " ", name).strip()
    return name


def name_key(name: str) -> str:
    """Normalised key for aggregating the same player across matches."""
    n = clean_name(name).lower()
    n = n.replace(".", " ")
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _num(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _get_person_name(obj) -> str:
    """Extract a name from either a {'name': ...} dict or a raw string."""
    if isinstance(obj, dict):
        return clean_name(obj.get("name") or obj.get("fullName") or "")
    return clean_name(obj)


# ---------------------------------------------------------------------------
# dismissal-text parsing (source of fielding credit + bowled/lbw bonus)
# ---------------------------------------------------------------------------
@dataclass
class DismissalCredit:
    catcher: Optional[str] = None
    stumper: Optional[str] = None
    runout_fielders: list = field(default_factory=list)
    bowled_or_lbw_bowler: Optional[str] = None


_NOT_OUT_MARKERS = ("not out", "did not bat", "dnb", "retired not out")


def parse_dismissal(text: str) -> DismissalCredit:
    """Derive fielding / bowling-mode credit from a dismissal string.

    Handles: ``c Fielder b Bowler``, ``c & b Bowler``, ``st Keeper b Bowler``,
    ``run out (A)``, ``run out (A/B)``, ``lbw b Bowler``, ``b Bowler``.
    """
    credit = DismissalCredit()
    raw = str(text or "").strip()
    low = raw.lower()

    if not raw or any(m in low for m in _NOT_OUT_MARKERS):
        return credit

    # Run out: names inside parentheses, separated by / or ,
    m = re.search(r"run\s*out\s*\(([^)]*)\)", raw, flags=re.IGNORECASE)
    if m or low.startswith("run out"):
        inside = m.group(1) if m else ""
        fielders = [clean_name(x) for x in re.split(r"[/,]", inside) if clean_name(x)]
        credit.runout_fielders = fielders[:2]  # last 2 fielders share the credit
        return credit

    # Caught & bowled -> the bowler is also the catcher
    m = re.match(r"c\s*(?:&|and)\s*b\s+(.+)$", raw, flags=re.IGNORECASE)
    if m:
        bowler = clean_name(m.group(1))
        credit.catcher = bowler
        return credit

    # Caught: "c Fielder b Bowler"
    m = re.match(r"c\s+(.+?)\s+b\s+(.+)$", raw, flags=re.IGNORECASE)
    if m:
        credit.catcher = clean_name(m.group(1))
        return credit

    # Stumped: "st Keeper b Bowler"
    m = re.match(r"st\s+(.+?)\s+b\s+(.+)$", raw, flags=re.IGNORECASE)
    if m:
        credit.stumper = clean_name(m.group(1))
        return credit

    # LBW: "lbw b Bowler"
    m = re.match(r"lbw\s+b\s+(.+)$", raw, flags=re.IGNORECASE)
    if m:
        credit.bowled_or_lbw_bowler = clean_name(m.group(1))
        return credit

    # Bowled: "b Bowler" (but not "c ... b ...", already handled above)
    m = re.match(r"b\s+(.+)$", raw, flags=re.IGNORECASE)
    if m:
        credit.bowled_or_lbw_bowler = clean_name(m.group(1))
        return credit

    return credit


# ---------------------------------------------------------------------------
# per-match parsing
# ---------------------------------------------------------------------------
class MatchAccumulator:
    """Collects every player's performance within a single match."""

    def __init__(self):
        self.perf: dict[str, scoring.MatchPerformance] = {}
        self.display: dict[str, str] = {}

    def _entry(self, name: str) -> scoring.MatchPerformance:
        key = name_key(name)
        if key not in self.perf:
            self.perf[key] = scoring.MatchPerformance()
            self.display[key] = clean_name(name)
        return self.perf[key]

    def add_batting(self, name: str, runs, fours, sixes, balls, dismissal_text):
        p = self._entry(name)
        b = p.batting
        b.runs += _num(runs)
        b.fours += _num(fours)
        b.sixes += _num(sixes)
        b.balls += _num(balls)
        low = str(dismissal_text or "").lower().strip()
        b.out = bool(low) and not any(m in low for m in _NOT_OUT_MARKERS)

    def add_bowling(self, name: str, wickets, maidens):
        p = self._entry(name)
        p.bowling.wickets += _num(wickets)
        p.bowling.maidens += _num(maidens)

    def add_catch(self, name: str):
        self._entry(name).fielding.catches += 1

    def add_stumping(self, name: str):
        self._entry(name).fielding.stumpings += 1

    def add_runout(self, fielders: list):
        if not fielders:
            return
        if len(fielders) == 1:
            self._entry(fielders[0]).fielding.runouts_direct += 1
        else:
            for f in fielders[:2]:
                self._entry(f).fielding.runouts_indirect += 1

    def add_bowled_lbw(self, bowler: str):
        self._entry(bowler).bowling.bowled_lbw += 1


def parse_scorecard(data: dict) -> MatchAccumulator:
    acc = MatchAccumulator()
    innings = data.get("scorecard") or []

    for inning in innings:
        # -- batting rows (also the source of all fielding credit) --
        for row in inning.get("batting") or []:
            batsman = _get_person_name(row.get("batsman") or row.get("name"))
            if not batsman:
                continue
            dismissal = (
                row.get("dismissal-text")
                or row.get("dismissal")
                or row.get("dismissalText")
                or ""
            )
            acc.add_batting(
                batsman,
                row.get("r", row.get("runs")),
                row.get("4s", row.get("fours")),
                row.get("6s", row.get("sixes")),
                row.get("b", row.get("balls")),
                dismissal,
            )

            credit = parse_dismissal(dismissal)
            dis_type = str(row.get("dismissal", "")).strip().lower()
            # The API sometimes fills `catcher` even on run-outs, so only credit
            # a catch when the dismissal is genuinely a catch. Prefer the
            # structured (full) name over the text, which may be a surname only.
            struct_catcher = _get_person_name(row.get("catcher")) if row.get("catcher") else ""
            catch_types = {"catch", "caught", "caught and bowled", "c and b", "c&b"}
            if credit.catcher is not None:
                acc.add_catch(struct_catcher or credit.catcher)
            elif dis_type in catch_types and struct_catcher:
                acc.add_catch(struct_catcher)

            if credit.stumper:
                acc.add_stumping(credit.stumper)
            if credit.runout_fielders:
                acc.add_runout(credit.runout_fielders)
            if credit.bowled_or_lbw_bowler:
                acc.add_bowled_lbw(credit.bowled_or_lbw_bowler)

        # -- bowling rows --
        for row in inning.get("bowling") or []:
            bowler = _get_person_name(row.get("bowler") or row.get("name"))
            if not bowler:
                continue
            acc.add_bowling(bowler, row.get("w", row.get("wickets")), row.get("m", row.get("maidens")))

    return acc


# ---------------------------------------------------------------------------
# season aggregation
# ---------------------------------------------------------------------------
class SeasonAggregate:
    def __init__(self):
        self.display: dict[str, str] = {}
        self.rows: dict[str, dict] = defaultdict(lambda: defaultdict(float))
        # One record per (match, player): base points scored in that match.
        self.match_player_rows: list[dict] = []

    def add_match(self, acc: MatchAccumulator, match_no: int = 0, match_label: str = "", match_date: str = ""):
        for key, perf in acc.perf.items():
            self.display.setdefault(key, acc.display[key])
            pts = scoring.match_points(perf, lineup_bonus=True)
            if pts["total"]:
                self.match_player_rows.append({
                    "MatchNo": match_no,
                    "Match": match_label,
                    "Date": match_date,
                    "Player": acc.display[key],
                    "MatchPoints": int(pts["total"]),
                })
            r = self.rows[key]
            r["Matches"] += 1
            # raw batting
            r["Runs"] += perf.batting.runs
            r["Balls"] += perf.batting.balls
            r["Fours"] += perf.batting.fours
            r["Sixes"] += perf.batting.sixes
            if perf.batting.runs >= 100:
                r["Hundreds"] += 1
            elif perf.batting.runs >= 50:
                r["Fifties"] += 1
            if perf.batting.out and perf.batting.runs == 0:
                r["Ducks"] += 1
            # raw bowling
            r["Wickets"] += perf.bowling.wickets
            r["Maidens"] += perf.bowling.maidens
            # raw fielding
            r["Catches"] += perf.fielding.catches
            r["Stumpings"] += perf.fielding.stumpings
            r["RunOuts"] += perf.fielding.runouts_direct + perf.fielding.runouts_indirect
            # points
            r["Batting_Points"] += pts["batting"]
            r["Bowling_Points"] += pts["bowling"]
            r["Fielding_Points"] += pts["fielding"]
            r["Lineup_Points"] += pts["other"]
            r["Base_Points"] += pts["total"]

    def to_frame(self) -> pd.DataFrame:
        records = []
        for key, r in self.rows.items():
            rec = {"Player": self.display.get(key, key)}
            rec.update({k: int(v) for k, v in r.items()})
            records.append(rec)
        if not records:
            return pd.DataFrame(
                columns=[
                    "Player", "Matches", "Runs", "Balls", "Fours", "Sixes",
                    "Fifties", "Hundreds", "Ducks", "Wickets", "Maidens",
                    "Catches", "Stumpings", "RunOuts", "Batting_Points",
                    "Bowling_Points", "Fielding_Points", "Lineup_Points",
                    "Base_Points",
                ]
            )
        df = pd.DataFrame.from_records(records)
        return df.sort_values("Base_Points", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# series discovery
# ---------------------------------------------------------------------------
def resolve_series_id(client: CricketDataClient) -> tuple[str, str]:
    """Return (series_id, series_name), auto-discovering when needed."""
    if config.HUNDRED_SERIES_ID:
        return config.HUNDRED_SERIES_ID, "(pinned via HUNDRED_SERIES_ID)"

    if config.RESOLVED_SERIES_FILE.exists():
        try:
            saved = json.loads(config.RESOLVED_SERIES_FILE.read_text(encoding="utf-8"))
            if saved.get("id"):
                return str(saved["id"]), str(saved.get("name", ""))
        except Exception:
            pass

    candidates = client.search_series(config.SERIES_SEARCH_TERM)
    if not candidates:
        raise CricketDataError(
            f"No series found matching '{config.SERIES_SEARCH_TERM}'. "
            "Set HUNDRED_SERIES_ID to pin one manually."
        )

    from datetime import date

    today = date.today()

    def start_date(row) -> Optional[date]:
        raw = str(row.get("startDate", "")).strip()
        try:  # API sometimes gives "2026-08-05", sometimes "Aug 05"
            return date.fromisoformat(raw)
        except ValueError:
            return None

    # Prefer an edition that has already started (start <= today), the latest
    # such; otherwise the earliest upcoming one. Falls back to most matches.
    started = [(start_date(c), c) for c in candidates]
    started = [(d, c) for d, c in started if d is not None]
    past = [(d, c) for d, c in started if d <= today]
    if past:
        best = max(past, key=lambda x: x[0])[1]
    elif started:
        best = min(started, key=lambda x: x[0])[1]
    else:
        best = max(candidates, key=lambda r: _num(r.get("matches"), 0))

    sid, name = str(best.get("id")), str(best.get("name", ""))
    save_series_choice(sid, name)
    return sid, name


def save_series_choice(series_id: str, name: str) -> None:
    try:
        config.RESOLVED_SERIES_FILE.write_text(
            json.dumps({"id": series_id, "name": name}, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def fetch_and_build() -> pd.DataFrame:
    client = CricketDataClient()
    series_id, series_name = resolve_series_id(client)
    print(f"Using series: {series_name} ({series_id})")

    info = client.series_info(series_id)
    match_list = info.get("matchList") or info.get("matches") or []
    print(f"Matches in series: {len(match_list)}")

    season = SeasonAggregate()
    match_records = []
    processed = 0

    # Process started matches in chronological order so match numbers line up.
    started_matches = [m for m in match_list if bool(m.get("matchStarted")) and m.get("id")]
    started_matches.sort(key=lambda m: str(m.get("dateTimeGMT") or m.get("date") or ""))

    def short_label(name: str, idx: int) -> str:
        # "MI London vs Sunrisers Leeds, 1st Match, ..." -> "M1: MI London v Sunrisers Leeds"
        teams = str(name).split(",")[0].replace(" vs ", " v ")
        return f"M{idx}: {teams}"

    for idx, m in enumerate(started_matches, start=1):
        match_id = str(m.get("id", ""))
        name = m.get("name", "")
        status = m.get("status", "")
        label = short_label(name, idx)
        mdate = str(m.get("date", ""))

        record = {"MatchNo": idx, "Match": name, "Label": label,
                  "Date": mdate, "Status": status, "Processed": False}
        scard = client.match_scorecard(match_id)
        if scard and scard.get("scorecard"):
            acc = parse_scorecard(scard)
            season.add_match(acc, match_no=idx, match_label=label, match_date=mdate)
            processed += 1
            record["Processed"] = True
            record["Status"] = scard.get("status", status)
        match_records.append(record)

    print(f"Scorecards processed: {processed}")
    if client.last_info:
        print(f"API hits today: {client.last_info.get('hitsToday')} / {client.last_info.get('hitsLimit')}")

    player_df = season.to_frame()

    # Cap tables for the dashboard "raw stats" view.
    top_runs = player_df.sort_values("Runs", ascending=False)[
        ["Player", "Matches", "Runs", "Fours", "Sixes"]
    ].head(30)
    top_wkts = player_df.sort_values("Wickets", ascending=False)[
        ["Player", "Matches", "Wickets", "Maidens"]
    ].head(30)
    matches_df = pd.DataFrame(match_records)
    match_player_df = pd.DataFrame(
        season.match_player_rows,
        columns=["MatchNo", "Match", "Date", "Player", "MatchPoints"],
    )

    with pd.ExcelWriter(config.PLAYER_STATS_FILE, engine="openpyxl") as writer:
        player_df.to_excel(writer, sheet_name="Player_Stats", index=False)
        top_runs.to_excel(writer, sheet_name="Top_Runs", index=False)
        top_wkts.to_excel(writer, sheet_name="Top_Wickets", index=False)
        matches_df.to_excel(writer, sheet_name="Matches", index=False)
        match_player_df.to_excel(writer, sheet_name="Match_Player_Points", index=False)

    print(f"Saved: {config.PLAYER_STATS_FILE}")
    return player_df


if __name__ == "__main__":
    fetch_and_build()
