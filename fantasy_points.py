"""Assign API player stats to the 5 fantasy squads and score each team.

Reads:
  * ``players.csv``                -- the 5 rosters (Player, Team, Role,
                                      Captain, ViceCaptain)
  * ``hundred_player_stats.xlsx``  -- base fantasy points per player from the
                                      CricketData scorecards (fetch_hundred_stats)

Writes ``Hundred_Fantasy_Points.xlsx`` with the scored rosters and leaderboard.

Roster names rarely match the API's spelling exactly, so a guarded fuzzy
matcher (surname must agree, protected against look-alike names) links each
roster entry to an API player. Captain (2x) and vice-captain (1.5x) multipliers
are applied to each player's season total.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

import pandas as pd
from rapidfuzz import process, fuzz

import config
import scoring

# Hand-maintained aliases correcting known roster misspellings to the API
# spelling. Keys are normalised (lowercase, no punctuation). Extend as new
# mismatches surface in the "not matched" list.
ALIASES: dict[str, str] = {
    "nikolas pooran": "nicholas pooran",
    "heinrich klaseen": "heinrich klaasen",
    "dewald bevis": "dewald brevis",
    "adien markram": "aiden markram",
    "donavan ferreria": "donovan ferreira",
    "marco jasen": "marco jansen",
    "lockie fergunson": "lockie ferguson",
    "josh tougue": "josh tongue",
    "jordan thompsons": "jordan thompson",
    "tom kohler": "tom kohler cadmore",
    "lhuan dra p": "lhuandre pretorius",
    "josh fillpi": "josh phillipe",
    "micheal pepper": "michael pepper",
    "jonny bairstow": "jonny bairstow",
}

COMMON_SURNAMES = {"singh", "sharma", "khan", "smith", "brown", "khan", "williams"}

# Names that must only ever match exactly (look-alikes), and pairs that must
# never be matched to each other. Populate as the league discovers clashes.
STRICT_NO_FUZZY: set[str] = set()
BLOCKED_MATCH_PAIRS: set[tuple[str, str]] = set()


def normalize_name(name: str) -> str:
    name = str(name).strip().lower()
    name = name.replace(".", " ")
    name = re.sub(r"\(.*?\)", "", name)  # drop any parenthetical (team code, wk)
    name = re.sub(r"[-|/]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def canonical_name(name: str) -> str:
    name = normalize_name(name)
    return ALIASES.get(name, name)


def tokenize(name: str) -> list[str]:
    return [t for t in canonical_name(name).split() if t]


def first_token(name: str) -> str:
    parts = tokenize(name)
    return parts[0] if parts else ""


def last_token(name: str) -> str:
    parts = tokenize(name)
    return parts[-1] if parts else ""


def initials(name: str) -> str:
    return "".join(p[0] for p in tokenize(name) if p)


def same_last_name(a: str, b: str) -> bool:
    return bool(last_token(a)) and last_token(a) == last_token(b)


def same_first_name(a: str, b: str) -> bool:
    return bool(first_token(a)) and first_token(a) == first_token(b)


def token_overlap_ratio(a: str, b: str) -> float:
    ta, tb = set(tokenize(a)), set(tokenize(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def first_name_similarity(a: str, b: str) -> int:
    return fuzz.ratio(first_token(a), first_token(b))


def is_blocked_pair(a: str, b: str) -> bool:
    return (canonical_name(a), canonical_name(b)) in BLOCKED_MATCH_PAIRS


def passes_structure_guard(player: str, candidate: str) -> bool:
    player, candidate = canonical_name(player), canonical_name(candidate)
    if is_blocked_pair(player, candidate):
        return False
    if not same_last_name(player, candidate):
        return False
    if player in STRICT_NO_FUZZY or candidate in STRICT_NO_FUZZY:
        return player == candidate
    if first_token(player) == first_token(candidate):
        return True
    if last_token(player) in COMMON_SURNAMES:
        return first_name_similarity(player, candidate) >= 90
    return first_name_similarity(player, candidate) >= 85


def ai_style_match(player: str, stats_names: list[str]) -> Tuple[Optional[str], str]:
    """Match a roster name to an API player name.

    Tolerant of misspelled surnames (common in the draft sheet) while guarding
    against ambiguity: a fuzzy win is only accepted outright when it clearly
    beats the runner-up, so look-alike names (Tom/Sam/Ben Curran) still resolve
    to the correct person via their first name.
    """
    player = canonical_name(player)
    if not stats_names:
        return None, "no_stats_yet"

    if player in stats_names:
        return player, "exact/alias"

    # Exact surname + matching first name (handles first-name typos like
    # nikolas/nicholas, adien/aiden).
    same_surname = [s for s in stats_names if same_last_name(player, s)]
    for s in same_surname:
        if same_first_name(player, s) or first_name_similarity(player, s) >= 78:
            if not is_blocked_pair(player, s):
                return s, "surname+firstname"

    # Fuzzy, but the SURNAME must be similar. Without this, a not-yet-played
    # player collapses onto a same-first-name stranger (e.g. david willey ->
    # david miller). Surname gating rejects those while still allowing
    # misspelled surnames (bevis/brevis, jasen/jansen, tougue/tongue).
    p_last = last_token(player)
    candidates = []
    for s in stats_names:
        if is_blocked_pair(player, s):
            continue
        surname_sim = fuzz.ratio(p_last, last_token(s))
        if surname_sim < 80 and last_token(s) != p_last:
            continue
        candidates.append((s, fuzz.token_sort_ratio(player, s)))
    if not candidates:
        return None, "no_stats_yet"

    candidates.sort(key=lambda x: x[1], reverse=True)
    top_name, top_score = candidates[0]
    second_score = candidates[1][1] if len(candidates) > 1 else 0

    if top_score >= 88:
        return top_name, f"fuzzy:{top_score}"
    if top_score >= 78 and (top_score - second_score) >= 6:
        return top_name, f"fuzzy_margin:{top_score}"
    if top_score >= 72:
        return None, f"possible_mismatch:{top_score}"
    return None, "no_stats_yet"


# ---------------------------------------------------------------------------
def _as_bool(value) -> bool:
    return str(value).strip().lower() in {"y", "yes", "true", "1", "c", "vc"}


def load_players() -> pd.DataFrame:
    df = pd.read_csv(config.PLAYERS_FILE)
    df.columns = df.columns.str.strip()

    required = {"Player", "Team", "Role"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"players.csv is missing columns: {missing}")

    for optional in ("Captain", "ViceCaptain"):
        if optional not in df.columns:
            df[optional] = ""

    df = df[df["Player"].astype(str).str.strip() != ""].copy()

    df["Player_Original"] = df["Player"].astype(str).str.strip()
    df["Player"] = df["Player"].apply(canonical_name)
    df["Team"] = df["Team"].astype(str).str.strip()
    df["Role"] = df["Role"].astype(str).str.strip().str.upper()
    df["Captain"] = df["Captain"].apply(_as_bool)
    df["ViceCaptain"] = df["ViceCaptain"].apply(_as_bool)
    return df


def load_stats() -> pd.DataFrame:
    stats = pd.read_excel(config.PLAYER_STATS_FILE, sheet_name="Player_Stats")
    stats.columns = stats.columns.str.strip()
    if stats.empty or "Player" not in stats.columns:
        return pd.DataFrame(columns=["Player", "Base_Points"])
    stats["match_key"] = stats["Player"].apply(canonical_name)
    # Collapse any duplicate spellings, keeping the richest row.
    stats = stats.sort_values("Base_Points", ascending=False).drop_duplicates("match_key")
    return stats


def match_players(players_df: pd.DataFrame, stats: pd.DataFrame) -> pd.DataFrame:
    stats_names = stats["match_key"].tolist() if "match_key" in stats.columns else []

    resolved, suggested, notes = [], [], []
    for player in players_df["Player"]:
        matched, note = ai_style_match(player, stats_names)
        if matched is not None:
            resolved.append(matched)
            suggested.append("")
        else:
            resolved.append("")
            guess = process.extractOne(player, stats_names, scorer=fuzz.ratio) if stats_names else None
            suggested.append(guess[0] if guess else "")
        notes.append(note)

    out = players_df.copy()
    out["Matched_Player"] = resolved
    out["Suggested_Match"] = suggested
    out["Match_Type"] = notes

    # Safety net: one API player must not be claimed by two roster entries.
    # If it happens, keep the strongest match (exact > structured > higher
    # fuzzy score) and release the weaker one.
    def match_rank(note: str) -> float:
        note = str(note)
        if note.startswith("exact"):
            return 1000.0
        if note.startswith("surname+firstname"):
            return 500.0
        if ":" in note:
            try:
                return float(note.split(":")[1])
            except ValueError:
                return 0.0
        return 0.0

    for name, idxs in out.groupby("Matched_Player").groups.items():
        if not str(name).strip() or len(idxs) < 2:
            continue
        ranked = sorted(idxs, key=lambda i: match_rank(out.at[i, "Match_Type"]), reverse=True)
        for i in ranked[1:]:  # release all but the best
            out.at[i, "Suggested_Match"] = out.at[i, "Matched_Player"]
            out.at[i, "Matched_Player"] = ""
            out.at[i, "Match_Type"] = "duplicate_release"
    return out


STAT_COLS = [
    "Matches", "Runs", "Wickets", "Catches", "Stumpings", "RunOuts", "Maidens",
    "Batting_Points", "Bowling_Points", "Fielding_Points", "Lineup_Points",
    "Base_Points",
]


def calculate_points(players_df: pd.DataFrame, stats: pd.DataFrame) -> pd.DataFrame:
    players_df = match_players(players_df, stats)

    stat_lookup = stats.set_index("match_key") if "match_key" in stats.columns else pd.DataFrame()

    merged = players_df.copy()
    for col in STAT_COLS:
        merged[col] = 0
    merged["API_Name"] = ""

    for idx, row in merged.iterrows():
        key = row["Matched_Player"]
        if key and not stat_lookup.empty and key in stat_lookup.index:
            srow = stat_lookup.loc[key]
            merged.at[idx, "API_Name"] = srow.get("Player", key)
            for col in STAT_COLS:
                if col in srow:
                    merged.at[idx, col] = srow[col]

    merged["Multiplier"] = 1.0
    merged.loc[merged["Captain"], "Multiplier"] = scoring.CAPTAIN_MULT
    merged.loc[merged["ViceCaptain"] & ~merged["Captain"], "Multiplier"] = scoring.VICE_CAPTAIN_MULT
    merged["Points"] = (merged["Base_Points"] * merged["Multiplier"]).round().astype(int)

    final = merged[
        [
            "Player_Original", "Team", "Role", "Captain", "ViceCaptain",
            "Matched_Player", "API_Name", "Suggested_Match", "Match_Type",
            "Matches", "Runs", "Wickets", "Catches", "Stumpings", "RunOuts",
            "Batting_Points", "Bowling_Points", "Fielding_Points",
            "Base_Points", "Multiplier", "Points",
        ]
    ].copy()
    final = final.rename(columns={"Player_Original": "Player"})
    for c in STAT_COLS + ["Points"]:
        if c in final.columns:
            final[c] = pd.to_numeric(final[c], errors="coerce").fillna(0)
    final["Base_Points"] = final["Base_Points"].astype(int)
    return final


def build_leaderboard(points_df: pd.DataFrame) -> pd.DataFrame:
    # Include every configured team even if it has no scoring players yet.
    totals = points_df.groupby("Team", as_index=False)["Points"].sum()
    for team in config.TEAMS:
        if team not in set(totals["Team"]):
            totals = pd.concat([totals, pd.DataFrame([{"Team": team, "Points": 0}])], ignore_index=True)
    leaderboard = totals.sort_values("Points", ascending=False).reset_index(drop=True)
    leaderboard.insert(0, "Rank", leaderboard.index + 1)
    leaderboard["Points"] = leaderboard["Points"].astype(int)
    return leaderboard


def build_match_team_points(points_df: pd.DataFrame) -> pd.DataFrame:
    """Points each team gained in each match (base points x captaincy).

    Reads the per-match, per-player table produced by fetch_hundred_stats and
    rolls it up to (match, team) using the roster match + captain multiplier.
    """
    cols = ["MatchNo", "Match", "Team", "Points"]
    try:
        mpp = pd.read_excel(config.PLAYER_STATS_FILE, sheet_name="Match_Player_Points")
    except Exception:
        return pd.DataFrame(columns=cols)
    if mpp.empty:
        return pd.DataFrame(columns=cols)

    # api canonical name -> (team, multiplier) from the resolved roster
    team_mult = {}
    for _, r in points_df.iterrows():
        key = str(r.get("Matched_Player", "")).strip()
        if key:
            team_mult[key] = (r["Team"], float(r.get("Multiplier", 1.0)))

    mpp["key"] = mpp["Player"].apply(canonical_name)
    mpp = mpp[mpp["key"].isin(set(team_mult))].copy()
    if mpp.empty:
        return pd.DataFrame(columns=cols)
    mpp["Team"] = mpp["key"].map(lambda k: team_mult[k][0])
    mpp["Points"] = mpp.apply(lambda r: r["MatchPoints"] * team_mult[r["key"]][1], axis=1)

    grouped = mpp.groupby(["MatchNo", "Match", "Team"], as_index=False)["Points"].sum()

    # Ensure every team has a bar for every played match (fill absent with 0).
    matches = grouped[["MatchNo", "Match"]].drop_duplicates()
    grid = matches.merge(pd.DataFrame({"Team": config.TEAMS}), how="cross")
    out = grid.merge(grouped, on=["MatchNo", "Match", "Team"], how="left").fillna({"Points": 0})
    out["Points"] = out["Points"].round().astype(int)
    return out.sort_values(["MatchNo", "Team"]).reset_index(drop=True)


def main() -> None:
    players_df = load_players()
    stats = load_stats()
    points_df = calculate_points(players_df, stats)
    leaderboard_df = build_leaderboard(points_df)
    match_team_df = build_match_team_points(points_df)

    no_stats_df = points_df[points_df["Match_Type"] == "no_stats_yet"].copy()
    mismatch_df = points_df[points_df["Match_Type"].astype(str).str.contains("possible_mismatch", na=False)].copy()
    ai_matches_df = points_df[points_df["Match_Type"].astype(str).str.contains("ai_", na=False)].copy()

    merged_stats = stats.drop(columns=["match_key"], errors="ignore")

    with pd.ExcelWriter(config.FANTASY_WORKBOOK, engine="openpyxl") as writer:
        points_df.to_excel(writer, sheet_name="Player_Points", index=False)
        leaderboard_df.to_excel(writer, sheet_name="Leaderboard", index=False)
        merged_stats.to_excel(writer, sheet_name="Merged_Stats", index=False)
        no_stats_df.to_excel(writer, sheet_name="No_Stats_Yet", index=False)
        mismatch_df.to_excel(writer, sheet_name="Possible_Mismatch", index=False)
        ai_matches_df.to_excel(writer, sheet_name="AI_Matches", index=False)
        match_team_df.to_excel(writer, sheet_name="Match_Team_Points", index=False)

    print(f"Saved: {config.FANTASY_WORKBOOK}")
    print(leaderboard_df.to_string(index=False))


if __name__ == "__main__":
    main()
