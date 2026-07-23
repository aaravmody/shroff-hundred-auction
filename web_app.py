from __future__ import annotations

import os
import subprocess
import sys
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

import config

try:
    from streamlit_autorefresh import st_autorefresh
    AUTO_REFRESH_AVAILABLE = True
except Exception:
    AUTO_REFRESH_AVAILABLE = False

try:
    import altair as alt
    ALTAIR_AVAILABLE = True
except Exception:
    ALTAIR_AVAILABLE = False


st.set_page_config(page_title="The Hundred Fantasy", page_icon="🏏", layout="wide")


def _resolve_api_key() -> str:
    """Find the CricketData key from the env var OR Streamlit secrets.

    On Streamlit Community Cloud the key is entered under *Secrets* and read via
    ``st.secrets``; we also mirror it into ``os.environ`` so config and the
    fetch subprocess (which read the env var) pick it up.
    """
    key = os.environ.get("CRICKET_DATA_API_KEY", "").strip()
    if not key:
        try:
            key = str(st.secrets["CRICKET_DATA_API_KEY"]).strip()
        except Exception:
            key = ""
    if key:
        os.environ["CRICKET_DATA_API_KEY"] = key
        config.CRICKET_DATA_API_KEY = key
    return key


_resolve_api_key()

st.markdown(
    """
    <style>
        .main > div { padding-top: 1rem; }
        .metric-card {
            background: linear-gradient(135deg, #1a0b2e 0%, #2d1b4e 100%);
            border: 1px solid rgba(255,255,255,0.08);
            padding: 1rem 1.1rem; border-radius: 18px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.18); min-height: 110px;
        }
        .metric-label { color: #cbd5e1; font-size: 0.95rem; margin-bottom: 0.35rem; }
        .metric-value { color: white; font-size: 1.7rem; font-weight: 700; line-height: 1.2; }
        .metric-sub { color: #a5b4fc; font-size: 0.88rem; margin-top: 0.35rem; }
        .pill-ok { display:inline-block; padding:0.18rem 0.6rem; border-radius:999px;
                   background:rgba(34,197,94,0.15); color:#22c55e; font-size:0.82rem; font-weight:600; }
        .pill-warn { display:inline-block; padding:0.18rem 0.6rem; border-radius:999px;
                     background:rgba(245,158,11,0.15); color:#f59e0b; font-size:0.82rem; font-weight:600; }
        .pill-bad { display:inline-block; padding:0.18rem 0.6rem; border-radius:999px;
                    background:rgba(239,68,68,0.15); color:#ef4444; font-size:0.82rem; font-weight:600; }
        .small-note { color:#94a3b8; font-size:0.88rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def fmt_dt(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    try:
        ts = pd.to_datetime(value, utc=True)
        return ts.tz_convert("Europe/London").strftime("%d %b %Y, %I:%M %p UK")
    except Exception:
        return str(value)


def team_label(code) -> str:
    return config.TEAM_NAMES.get(str(code), str(code))


def with_team_names(df: pd.DataFrame) -> pd.DataFrame:
    if not df.empty and "Team" in df.columns:
        df = df.copy()
        df["Team"] = df["Team"].map(team_label)
    return df


def team_color_scale():
    """Altair colour scale keyed on full team names, consistent everywhere."""
    domain = [config.TEAM_NAMES[c] for c in config.TEAMS]
    rng = [config.TEAM_COLORS[c] for c in config.TEAMS]
    return alt.Scale(domain=domain, range=rng)


TEAM_LEGEND = alt.Legend(orient="bottom", title=None, columns=3, labelLimit=220)


def status_pill(state: str) -> str:
    s = str(state).strip().lower()
    if s in {"ok", "healthy", "running", "success", "up"}:
        return '<span class="pill-ok">Healthy</span>'
    if s in {"warning", "degraded", "stale", "starting"}:
        return '<span class="pill-warn">Warning</span>'
    return '<span class="pill-bad">Issue</span>'


@st.cache_data(ttl=60)
def load_sheet(sheet_name: str) -> pd.DataFrame:
    if not config.FANTASY_WORKBOOK.exists():
        return pd.DataFrame()
    try:
        return pd.read_excel(config.FANTASY_WORKBOOK, sheet_name=sheet_name)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=60)
def load_stats_sheet(sheet_name: str) -> pd.DataFrame:
    if not config.PLAYER_STATS_FILE.exists():
        return pd.DataFrame()
    try:
        return pd.read_excel(config.PLAYER_STATS_FILE, sheet_name=sheet_name)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=60)
def load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=30)
def load_status() -> dict:
    if not config.STATUS_FILE.exists():
        return {}
    try:
        return json.loads(config.STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


ROSTER_COLUMNS = ["Player", "Team", "Role", "Captain", "ViceCaptain"]


def load_roster() -> pd.DataFrame:
    if not config.PLAYERS_FILE.exists():
        return pd.DataFrame(columns=ROSTER_COLUMNS)
    df = pd.read_csv(config.PLAYERS_FILE)
    df.columns = df.columns.str.strip()
    for col in ROSTER_COLUMNS:
        if col not in df.columns:
            df[col] = "" if col in ("Player", "Team", "Role") else False
    for col in ("Captain", "ViceCaptain"):
        df[col] = df[col].apply(lambda v: str(v).strip().lower() in {"y", "yes", "true", "1"})
    df["Player"] = df["Player"].astype(str).str.strip()
    df["Team"] = df["Team"].astype(str).str.strip()
    df["Role"] = df["Role"].astype(str).str.strip().str.upper()
    df = df[df["Player"] != ""]
    return df[ROSTER_COLUMNS].reset_index(drop=True)


def save_roster(df: pd.DataFrame) -> None:
    out = df.copy()[ROSTER_COLUMNS]
    out["Captain"] = out["Captain"].map(lambda v: "Y" if bool(v) else "")
    out["ViceCaptain"] = out["ViceCaptain"].map(lambda v: "Y" if bool(v) else "")
    out.to_csv(config.PLAYERS_FILE, index=False)


def fmt_history(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["snapshot_time"] = pd.to_datetime(df["snapshot_time"], utc=True, errors="coerce")
    return df.dropna(subset=["snapshot_time"])


# ----------------------------------------------------------------------------
# Sidebar
# ----------------------------------------------------------------------------
with st.sidebar:
    st.header("The Hundred Fantasy")
    st.caption("5 teams · 15 players each")

    auto_refresh = st.toggle("Auto refresh", value=True)
    refresh_seconds = st.slider("Refresh interval (s)", 30, 300, 60, 30)
    if auto_refresh and AUTO_REFRESH_AVAILABLE:
        st_autorefresh(interval=refresh_seconds * 1000, key="refresh")

    st.markdown("---")
    st.subheader("Data")
    api_key_present = bool(config.CRICKET_DATA_API_KEY)
    st.markdown(
        f"CricketData API key: {status_pill('ok') if api_key_present else status_pill('bad')}",
        unsafe_allow_html=True,
    )
    if config.RESOLVED_SERIES_FILE.exists():
        try:
            series = json.loads(config.RESOLVED_SERIES_FILE.read_text(encoding="utf-8"))
            st.caption(f"Series: {series.get('name', '?')}")
        except Exception:
            pass

    if st.button("🔄 Refresh data now", disabled=not api_key_present,
                 help="Runs the CricketData fetch + scoring pipeline"):
        with st.spinner("Fetching latest stats and scoring squads…"):
            r1 = subprocess.run([sys.executable, "fetch_hundred_stats.py"],
                                cwd=config.BASE_DIR, capture_output=True, text=True)
            r2 = subprocess.run([sys.executable, "fantasy_points.py"],
                                cwd=config.BASE_DIR, capture_output=True, text=True)
        st.cache_data.clear()
        if r1.returncode == 0 and r2.returncode == 0:
            st.success("Updated.")
        else:
            st.error("Update failed — see logs below.")
            st.code((r1.stderr or "") + "\n" + (r2.stderr or ""), language="text")


# ----------------------------------------------------------------------------
# Load data
# ----------------------------------------------------------------------------
player_points_df = load_sheet("Player_Points")
leaderboard_df = load_sheet("Leaderboard")
no_stats_df = load_sheet("No_Stats_Yet")
mismatch_df = load_sheet("Possible_Mismatch")
top_runs_df = load_stats_sheet("Top_Runs")
top_wkts_df = load_stats_sheet("Top_Wickets")
matches_df = load_stats_sheet("Matches")
match_team_df = load_sheet("Match_Team_Points")
history_df = load_csv(config.LEADERBOARD_HISTORY_FILE)
status_data = load_status()

st.title("🏏 The Hundred — Fantasy Live Dashboard")
st.caption("Live leaderboard, detailed points breakdown, and squad management")

if leaderboard_df.empty and player_points_df.empty:
    st.info(
        "No scored data yet. Add players to the 5 squads in the **Manage Squads** tab, "
        "then run the pipeline (sidebar → *Refresh data now*, or the scheduled GitHub Action) "
        "once The Hundred matches begin."
    )


# ----------------------------------------------------------------------------
# Summary cards
# ----------------------------------------------------------------------------
leader_name, leader_sub, leader_color = "—", "—", "#ffffff"
if not leaderboard_df.empty and {"Team", "Points"}.issubset(leaderboard_df.columns):
    lb_sorted = leaderboard_df.sort_values("Points", ascending=False).reset_index(drop=True)
    top = lb_sorted.iloc[0]
    leader_name = team_label(top["Team"])
    leader_color = config.TEAM_COLORS.get(str(top["Team"]), "#ffffff")
    lead_pts = int(top["Points"])
    if len(lb_sorted) >= 2:
        margin = lead_pts - int(lb_sorted.iloc[1]["Points"])
        leader_sub = f"{lead_pts} pts · +{margin} ahead"
    else:
        leader_sub = f"{lead_pts} pts"

top_player_name, top_player_sub = "—", "—"
if not player_points_df.empty and {"Player", "Points"}.issubset(player_points_df.columns):
    p = player_points_df.sort_values("Points", ascending=False).iloc[0]
    top_player_name = str(p["Player"]).title()
    top_player_sub = f"{int(p['Points'])} pts · {team_label(p.get('Team', ''))}"

matches_played = 0
if not matches_df.empty and "Processed" in matches_df.columns:
    matches_played = int(matches_df["Processed"].sum())

c1, c2, c3, c4 = st.columns(4)
cards = [
    ("🏆 Current Leader", leader_name, leader_sub, leader_color),
    ("⭐ Top Player", top_player_name, top_player_sub, "#ffffff"),
    ("🏏 Matches Scored", str(matches_played), "from CricketData", "#ffffff"),
    ("👥 Teams", str(config.NUM_TEAMS), f"{config.SQUAD_SIZE} players each", "#ffffff"),
]
for col, (label, value, sub, colour) in zip((c1, c2, c3, c4), cards):
    with col:
        st.markdown(
            f"""<div class="metric-card" style="border-left:4px solid {colour}">
            <div class="metric-label">{label}</div>
            <div class="metric-value" style="color:{colour}">{value}</div>
            <div class="metric-sub">{sub}</div></div>""",
            unsafe_allow_html=True,
        )

s1, s2, s3, s4 = st.columns(4)
with s1:
    st.markdown("**Last Update**")
    st.markdown(f"<div class='small-note'>{fmt_dt(status_data.get('last_successful_scrape_time'))}</div>",
                unsafe_allow_html=True)
with s2:
    st.markdown("**Last Push**")
    st.markdown(f"<div class='small-note'>{fmt_dt(status_data.get('last_successful_git_push_time'))}</div>",
                unsafe_allow_html=True)
with s3:
    st.markdown("**Server**")
    st.markdown(status_pill(status_data.get("server_status", "unknown")), unsafe_allow_html=True)
with s4:
    st.markdown("**Data Source**")
    st.markdown(status_pill(status_data.get("data_source_status", "unknown")), unsafe_allow_html=True)

st.markdown("---")


# ----------------------------------------------------------------------------
# Tabs
# ----------------------------------------------------------------------------
tab_lead, tab_players, tab_squads, tab_raw, tab_rules = st.tabs(
    ["🏆 Leaderboard", "👤 Player Points", "🛠️ Manage Squads", "📊 Raw Stats", "📖 Scoring Rules"]
)

# ---- Leaderboard tab ----
with tab_lead:
    st.subheader("Team Leaderboard")
    if not leaderboard_df.empty and {"Team", "Points"}.issubset(leaderboard_df.columns):
        lb_display = with_team_names(leaderboard_df)
        left, right = st.columns((0.9, 1.1))
        with left:
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            lb_table = lb_display.copy()
            lb_table["Rank"] = lb_table["Rank"].map(lambda r: medals.get(int(r), f"{int(r)}"))
            lb_table = lb_table.rename(columns={"Rank": ""})
            st.dataframe(
                lb_table, width='stretch', hide_index=True, height=250,
                column_config={"Points": st.column_config.ProgressColumn(
                    "Points", format="%d",
                    min_value=0, max_value=int(max(lb_display["Points"].max(), 1)))},
            )
        with right:
            if ALTAIR_AVAILABLE:
                chart = (
                    alt.Chart(lb_display).mark_bar(cornerRadiusTopRight=6, cornerRadiusBottomRight=6)
                    .encode(
                        x=alt.X("Points:Q", title=None),
                        y=alt.Y("Team:N", sort="-x", title=None),
                        color=alt.Color("Team:N", scale=team_color_scale(), legend=None),
                        tooltip=["Team", "Points"],
                    )
                    .properties(height=250)
                )
                st.altair_chart(chart, width='stretch')
            else:
                st.bar_chart(lb_display.set_index("Team")["Points"])
    else:
        st.info("Leaderboard appears once squads have scoring players.")

    # ---- Points gained per match (the requested chart) ----
    st.subheader("Points Gained Per Match")
    if not match_team_df.empty and {"MatchNo", "Match", "Team", "Points"}.issubset(match_team_df.columns):
        mt = with_team_names(match_team_df).sort_values("MatchNo")
        match_order = mt.drop_duplicates("MatchNo").sort_values("MatchNo")["Match"].tolist()
        if ALTAIR_AVAILABLE:
            chart = (
                alt.Chart(mt).mark_bar()
                .encode(
                    x=alt.X("Match:N", sort=match_order, title=None,
                            axis=alt.Axis(labelAngle=0, labelLimit=200)),
                    xOffset=alt.XOffset("Team:N", sort=[config.TEAM_NAMES[c] for c in config.TEAMS]),
                    y=alt.Y("Points:Q", title="Points gained"),
                    color=alt.Color("Team:N", scale=team_color_scale(), legend=TEAM_LEGEND),
                    tooltip=["Match", "Team", "Points"],
                )
                .properties(height=380)
            )
            st.altair_chart(chart, width='stretch')
        else:
            st.bar_chart(mt.pivot_table(index="Match", columns="Team", values="Points"))
        st.caption("How many fantasy points each team earned in each completed match "
                   "(captain / vice-captain multipliers included).")
    else:
        st.info("This chart populates as matches are played.")

    # ---- Cumulative points over time ----
    st.subheader("Team Points Over Time")
    if not history_df.empty and {"snapshot_time", "Team", "Points"}.issubset(history_df.columns):
        hist = with_team_names(fmt_history(history_df))
        if ALTAIR_AVAILABLE and not hist.empty:
            chart = (
                alt.Chart(hist).mark_line(point=True)
                .encode(x=alt.X("snapshot_time:T", title=None), y=alt.Y("Points:Q", title="Total points"),
                        color=alt.Color("Team:N", scale=team_color_scale(), legend=TEAM_LEGEND),
                        tooltip=["Team", "snapshot_time", "Points"])
                .properties(height=340)
            )
            st.altair_chart(chart, width='stretch')
        else:
            st.line_chart(hist.pivot_table(index="snapshot_time", columns="Team", values="Points"))
    else:
        st.info("Trend line builds up as the tournament progresses.")

# ---- Player Points tab ----
with tab_players:
    st.subheader("Player Points Explorer")
    if player_points_df.empty:
        st.info("No player points yet.")
    else:
        f1, f2, f3 = st.columns(3)
        with f1:
            teams = sorted(player_points_df["Team"].dropna().unique().tolist())
            sel_teams = st.multiselect("Team", teams, default=teams, format_func=team_label)
        with f2:
            roles = sorted(player_points_df["Role"].dropna().unique().tolist())
            sel_roles = st.multiselect("Role", roles, default=roles)
        with f3:
            search = st.text_input("Search player")

        view = player_points_df.copy()
        if sel_teams:
            view = view[view["Team"].isin(sel_teams)]
        if sel_roles:
            view = view[view["Role"].isin(sel_roles)]
        if search:
            view = view[view["Player"].astype(str).str.contains(search, case=False, na=False)]

        def tag(row):
            if row.get("Captain"):
                return "🅲"
            if row.get("ViceCaptain"):
                return "🆅🅲"
            return ""
        if {"Captain", "ViceCaptain"}.issubset(view.columns):
            view = view.copy()
            view.insert(1, "Role Tag", view.apply(tag, axis=1))

        show_cols = [c for c in [
            "Player", "Role Tag", "Team", "Role", "Matches", "Runs", "Wickets",
            "Catches", "Batting_Points", "Bowling_Points", "Fielding_Points",
            "Base_Points", "Multiplier", "Points",
        ] if c in view.columns]
        st.dataframe(
            with_team_names(view[show_cols]).sort_values("Points", ascending=False),
            width='stretch', hide_index=True, height=520,
        )
        st.caption("🅲 = Captain (2× points) · 🆅🅲 = Vice-Captain (1.5× points). "
                   "Base_Points already include the +4 in-lineup bonus per match played.")

# ---- Manage Squads tab ----
with tab_squads:
    st.subheader("Manage Squads")
    roster = load_roster()

    counts = {t: int((roster["Team"] == t).sum()) for t in config.TEAMS} if not roster.empty else {t: 0 for t in config.TEAMS}
    cols = st.columns(config.NUM_TEAMS)
    for col, team in zip(cols, config.TEAMS):
        with col:
            n = counts.get(team, 0)
            colour = "#22c55e" if n == config.SQUAD_SIZE else ("#f59e0b" if n > 0 else "#94a3b8")
            st.markdown(
                f"<div class='metric-card'><div class='metric-label'>{team_label(team)}</div>"
                f"<div class='metric-value' style='color:{colour}'>{n}/{config.SQUAD_SIZE}</div>"
                f"<div class='metric-sub'>players</div></div>",
                unsafe_allow_html=True,
            )

    st.markdown("### ➕ Add a Player")
    with st.form("add_player", clear_on_submit=True):
        a1, a2, a3 = st.columns(3)
        with a1:
            new_name = st.text_input("Player name")
            new_team = st.selectbox("Team", config.TEAMS, format_func=team_label)
        with a2:
            new_role = st.selectbox("Role", ["BAT", "BOWL", "AR", "WK"])
            is_captain = st.checkbox("Captain (2×)")
        with a3:
            is_vc = st.checkbox("Vice-Captain (1.5×)")
        submitted = st.form_submit_button("Add player")

    if submitted:
        name = new_name.strip()
        err = None
        team_players = roster[roster["Team"] == new_team]
        if not name:
            err = "Enter a player name."
        elif len(team_players) >= config.SQUAD_SIZE:
            err = f"{team_label(new_team)} already has {config.SQUAD_SIZE} players."
        elif (team_players["Player"].str.lower() == name.lower()).any():
            err = f"{name} is already in {team_label(new_team)}."
        elif is_captain and is_vc:
            err = "A player can be either Captain or Vice-Captain, not both."

        if err:
            st.error(err)
        else:
            new_row = {"Player": name, "Team": new_team, "Role": new_role,
                       "Captain": is_captain, "ViceCaptain": is_vc}
            updated = roster.copy()
            # Enforce a single captain / vice-captain per team.
            if is_captain:
                updated.loc[updated["Team"] == new_team, "Captain"] = False
            if is_vc:
                updated.loc[updated["Team"] == new_team, "ViceCaptain"] = False
            updated = pd.concat([updated, pd.DataFrame([new_row])], ignore_index=True)
            save_roster(updated)
            st.cache_data.clear()
            st.success(f"Added {name} to {team_label(new_team)}. Re-run the pipeline to score them.")
            st.rerun()

    st.markdown("### ✏️ Edit / Remove Players")
    st.caption("Tick **Remove** and click *Save changes* to delete rows. You can also edit "
               "team, role and captaincy inline. One Captain and one Vice-Captain per team.")
    roster = load_roster()
    if roster.empty:
        st.info("No players yet — add some above.")
    else:
        team_full_names = [config.TEAM_NAMES[c] for c in config.TEAMS]
        name_to_code = {config.TEAM_NAMES[c]: c for c in config.TEAMS}
        edit_df = roster.copy()
        edit_df["Team"] = edit_df["Team"].map(team_label)  # show full names
        edit_df["Remove"] = False
        edited = st.data_editor(
            edit_df,
            width='stretch', hide_index=True, height=460, key="roster_editor",
            column_config={
                "Team": st.column_config.SelectboxColumn(options=team_full_names),
                "Role": st.column_config.SelectboxColumn(options=["BAT", "BOWL", "AR", "WK"]),
                "Captain": st.column_config.CheckboxColumn(),
                "ViceCaptain": st.column_config.CheckboxColumn(),
                "Remove": st.column_config.CheckboxColumn(),
            },
        )
        if st.button("💾 Save changes"):
            kept = edited[~edited["Remove"]].drop(columns=["Remove"]).copy()
            kept["Team"] = kept["Team"].map(lambda n: name_to_code.get(n, n))  # back to codes
            problems = []
            for team in config.TEAMS:
                tp = kept[kept["Team"] == team]
                if len(tp) > config.SQUAD_SIZE:
                    problems.append(f"{team_label(team)} has {len(tp)} players (max {config.SQUAD_SIZE}).")
                if tp["Captain"].sum() > 1:
                    problems.append(f"{team_label(team)} has more than one Captain.")
                if tp["ViceCaptain"].sum() > 1:
                    problems.append(f"{team_label(team)} has more than one Vice-Captain.")
            if problems:
                st.error(" ".join(problems))
            else:
                save_roster(kept)
                st.cache_data.clear()
                st.success("Roster saved. Re-run the pipeline to rescore.")
                st.rerun()

    st.markdown("---")
    st.warning(
        "**Persistence note:** on Streamlit Community Cloud the filesystem resets on restart/redeploy, "
        "so squad edits made here are not permanent and are **not** committed to GitHub automatically. "
        "Download the updated roster below and commit it to the repo to make changes permanent."
    )
    if config.PLAYERS_FILE.exists():
        st.download_button(
            "⬇️ Download players.csv",
            data=config.PLAYERS_FILE.read_bytes(),
            file_name="players.csv",
            mime="text/csv",
        )

    unmatched = pd.concat([no_stats_df, mismatch_df], ignore_index=True) if not (no_stats_df.empty and mismatch_df.empty) else pd.DataFrame()
    if not unmatched.empty:
        with st.expander("⚠️ Players not matched to API stats"):
            cols = [c for c in ["Player", "Team", "Role", "Suggested_Match", "Match_Type"] if c in unmatched.columns]
            st.dataframe(with_team_names(unmatched[cols]), width='stretch', hide_index=True)
            st.caption("These roster names could not be linked to a CricketData player. "
                       "Fix the spelling here or add an alias in fantasy_points.py.")

# ---- Raw Stats tab ----
with tab_raw:
    st.subheader("Raw Tournament Stats")
    r1, r2 = st.columns(2)
    with r1:
        st.markdown("**Most Runs**")
        st.dataframe(top_runs_df, width='stretch', hide_index=True, height=420) if not top_runs_df.empty else st.info("No batting data yet.")
    with r2:
        st.markdown("**Most Wickets**")
        st.dataframe(top_wkts_df, width='stretch', hide_index=True, height=420) if not top_wkts_df.empty else st.info("No bowling data yet.")

    st.markdown("**Matches**")
    if not matches_df.empty:
        st.dataframe(matches_df, width='stretch', hide_index=True, height=320)
    else:
        st.info("Match list appears once the series is resolved from the API.")

# ---- Rules tab ----
with tab_rules:
    st.subheader("Points Scoring")
    rc1, rc2 = st.columns(2)
    with rc1:
        st.markdown("""
**Batting**
| Event | Points |
|---|---|
| Run | +1 |
| Boundary bonus (per four) | +1 |
| Six bonus (per six) | +2 |
| 30 run bonus | +5 |
| 50 bonus | +10 |
| 100 bonus | +20 |
| Duck (out for 0) | −2 |

*A hundred earns only the +20 (not the 30/50 bonus).*

**Fielding**
| Event | Points |
|---|---|
| Catch | +8 |
| 3-catch bonus | +4 |
| Stumping | +12 |
| Run out (direct hit) | +12 |
| Run out (not direct) | +6 |
        """)
    with rc2:
        st.markdown("""
**Bowling**
| Event | Points |
|---|---|
| Wicket (excl. run out) | +25 |
| LBW / Bowled bonus | +8 |
| 2 wicket bonus | +3 |
| 3 wicket bonus | +5 |
| 4 wicket bonus | +10 |
| 5 wicket bonus | +20 |
| Maiden | +12 |

*A five-for earns only the +20 (not the 2/3/4 bonuses).*

**Other**
| Event | Multiplier / Points |
|---|---|
| Captain | 2× points |
| Vice-Captain | 1.5× points |
| In announced line-up | +4 per match |
        """)
    st.caption("Fielding credit (catches / stumpings / run-outs) is derived from each innings' "
               "dismissal descriptions in the CricketData scorecard.")

st.markdown("---")
st.caption("Data: CricketData API · The Hundred fantasy league · 5 teams × 15 players")
