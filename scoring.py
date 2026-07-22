"""The Hundred fantasy scoring engine.

Points are computed **per match, per player** because several bonuses depend on
the single-innings/single-match total (milestones, wicket hauls, the 3-catch
bonus). Season totals are the sum of per-match points.

Point system
------------
BATTING
    Run                     +1  (each run)
    Boundary Bonus          +1  (each four)
    Six Bonus               +2  (each six)
    30 Run Bonus            +5
    50 Bonus               +10
    100 Bonus              +20
    Duck (out for 0)        -2

    Milestones stack up to 99 (a 50 also earns the 30 bonus), but a hundred
    earns ONLY the +20 (per the league note: "This will not include their 30
    and 50 bonus").

BOWLING
    Wicket (excl. run out)  +25  (each)
    Bonus (LBW/Bowled)      +8   (each such wicket)
    2 Wicket Bonus          +3
    3 Wicket Bonus          +5
    4 Wicket Bonus         +10
    5 Wicket Bonus         +20
    Maiden                 +12  (each)

    Haul bonuses stack up to 4 wickets, but a 5-fer earns ONLY the +20 (per the
    league note: "No points will be awarded to them for the 2, 3 or 4 Wicket
    Bonuses").

    NOTE ON MAIDENS: the CricketData scorecard for The Hundred does not report a
    maiden count (bowling rows give balls/dots, not maidens), so maiden points
    are currently 0. Detecting a maiden (a bowler's set of 5 with no runs) would
    require the ball-by-ball feed.

FIELDING
    Catch                   +8   (each)
    3 Catch Bonus           +4   (once, if catches >= 3)
    Stumping               +12   (each)
    Run Out (Direct Hit)   +12   (each)
    Run Out (Not Direct)    +6   (each; awarded to the last 2 fielders)

OTHER
    Captain                2x on the player's total
    Vice-Captain          1.5x on the player's total
    In Announced Line Up    +4  per match the player featured
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---- batting constants ----
RUN = 1
BOUNDARY_BONUS = 1
SIX_BONUS = 2
BONUS_30 = 5
BONUS_50 = 10
BONUS_100 = 20
DUCK = -2

# ---- bowling constants ----
WICKET = 25
BOWLED_LBW_BONUS = 8
HAUL_2 = 3
HAUL_3 = 5
HAUL_4 = 10
HAUL_5 = 20
MAIDEN = 12

# ---- fielding constants ----
CATCH = 8
THREE_CATCH_BONUS = 4
STUMPING = 12
RUNOUT_DIRECT = 12
RUNOUT_INDIRECT = 6

# ---- other ----
CAPTAIN_MULT = 2.0
VICE_CAPTAIN_MULT = 1.5
LINEUP_BONUS = 4


@dataclass
class BattingLine:
    runs: int = 0
    fours: int = 0
    sixes: int = 0
    balls: int = 0
    out: bool = False  # dismissed (needed to distinguish a duck from not-out 0)


@dataclass
class BowlingLine:
    wickets: int = 0        # excludes run outs (API bowling 'w' already does)
    bowled_lbw: int = 0     # how many of those wickets were bowled or lbw
    maidens: int = 0


@dataclass
class FieldingLine:
    catches: int = 0
    stumpings: int = 0
    runouts_direct: int = 0
    runouts_indirect: int = 0


@dataclass
class MatchPerformance:
    """One player's full contribution in a single match."""

    batting: BattingLine = field(default_factory=BattingLine)
    bowling: BowlingLine = field(default_factory=BowlingLine)
    fielding: FieldingLine = field(default_factory=FieldingLine)
    in_lineup: bool = True  # featured in this match (present in the scorecard)


def batting_points(b: BattingLine) -> int:
    pts = b.runs * RUN
    pts += b.fours * BOUNDARY_BONUS
    pts += b.sixes * SIX_BONUS

    # Milestone bonuses. A hundred replaces the 30/50 bonuses entirely.
    if b.runs >= 100:
        pts += BONUS_100
    elif b.runs >= 50:
        pts += BONUS_50 + BONUS_30
    elif b.runs >= 30:
        pts += BONUS_30

    if b.out and b.runs == 0:
        pts += DUCK

    return pts


def bowling_points(bw: BowlingLine) -> int:
    pts = bw.wickets * WICKET
    pts += bw.bowled_lbw * BOWLED_LBW_BONUS
    pts += bw.maidens * MAIDEN

    # Haul bonuses. A five-for replaces the 2/3/4 bonuses entirely.
    w = bw.wickets
    if w >= 5:
        pts += HAUL_5
    elif w == 4:
        pts += HAUL_2 + HAUL_3 + HAUL_4
    elif w == 3:
        pts += HAUL_2 + HAUL_3
    elif w == 2:
        pts += HAUL_2

    return pts


def fielding_points(f: FieldingLine) -> int:
    pts = f.catches * CATCH
    if f.catches >= 3:
        pts += THREE_CATCH_BONUS
    pts += f.stumpings * STUMPING
    pts += f.runouts_direct * RUNOUT_DIRECT
    pts += f.runouts_indirect * RUNOUT_INDIRECT
    return pts


def match_points(perf: MatchPerformance, lineup_bonus: bool = True) -> dict:
    """Return the base (pre-captain/VC) point breakdown for one match."""
    bat = batting_points(perf.batting)
    bowl = bowling_points(perf.bowling)
    field = fielding_points(perf.fielding)
    other = LINEUP_BONUS if (lineup_bonus and perf.in_lineup) else 0
    return {
        "batting": bat,
        "bowling": bowl,
        "fielding": field,
        "other": other,
        "total": bat + bowl + field + other,
    }


def apply_captaincy(points: float, is_captain: bool, is_vice_captain: bool) -> float:
    """Apply the captain / vice-captain multiplier to a season total."""
    if is_captain:
        return points * CAPTAIN_MULT
    if is_vice_captain:
        return points * VICE_CAPTAIN_MULT
    return points
