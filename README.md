# The Hundred — Fantasy League

A fantasy cricket dashboard for **The Hundred**. Player stats come from the
[CricketData API](https://cricketdata.org) (cricapi.com), are scored with the
league's detailed point system, and shown on a Streamlit dashboard.

- **5 teams · 15 players each**
- Squads are built and edited from the **Manage Squads** tab in the app.
- Points update automatically after each match day.

## How it fits together

| File | Role |
|------|------|
| `config.py` | Team names, file paths, API settings (all knobs in one place) |
| `cricket_data_api.py` | Thin, cached client for the CricketData API |
| `scoring.py` | The point system (batting / bowling / fielding / bonuses) |
| `fetch_hundred_stats.py` | Pulls the series + scorecards → `hundred_player_stats.xlsx` |
| `fantasy_points.py` | Maps rosters (`players.csv`) onto stats → `Hundred_Fantasy_Points.xlsx` |
| `auto_update_and_push.py` | One update cycle: fetch → score → snapshot → commit/push |
| `web_app.py` | Streamlit dashboard + squad management |
| `.github/workflows/update.yml` | Hourly scheduled update |

## Setup

1. Get a free API key from https://cricketdata.org and set it as an environment
   variable:
   ```bash
   export CRICKET_DATA_API_KEY="your-key"        # Windows PowerShell: $env:CRICKET_DATA_API_KEY="your-key"
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the app:
   ```bash
   streamlit run web_app.py
   ```

### Series discovery
On first run the pipeline searches CricketData for **"The Hundred"** and picks
the edition with the most matches, remembering the choice in
`hundred_series.json`. To pin a specific series instead, set
`HUNDRED_SERIES_ID`.

## Building the squads
Open the **Manage Squads** tab and use **Add a Player** (name, team, role,
optional Captain/Vice-Captain). Names are fuzzy-matched to CricketData players,
so spell the **surname** correctly; if a name won't match, add an entry to
`ALIASES` in `fantasy_points.py`.

> **Persistence:** on Streamlit Community Cloud the filesystem resets on
> restart, so edits made in the app are not permanent. Use the **Download
> players.csv** button and commit the file to the repo to keep changes.

## Deploying (Streamlit Community Cloud)
1. Push this repo to GitHub.
2. Create a new Streamlit app pointing at `web_app.py`.
3. In the app's **Secrets**, add:
   ```toml
   CRICKET_DATA_API_KEY = "your-key"
   ```
4. Add the same value as a GitHub Actions **secret** (`CRICKET_DATA_API_KEY`)
   so the scheduled workflow can refresh data. Optionally set a repo
   **variable** `HUNDRED_SERIES_ID`.

## Point system
See the **Scoring Rules** tab in the app for the full table. Highlights:
batting (runs +1, four +1, six +2, 30/50/100 milestones, duck −2), bowling
(wicket +25, bowled/lbw +8, 2/3/4/5-wicket hauls, maiden +12), fielding (catch
+8, 3-catch +4, stumping +12, run-out direct +12 / indirect +6), plus captain
(2×), vice-captain (1.5×) and +4 per match in the line-up. Fielding credit is
parsed from each innings' dismissal descriptions.

**Known limitation — maidens:** the CricketData scorecard for The Hundred does
not expose a maiden count (only balls/dots per bowler), so maiden points are
currently 0. Everything else (runs, boundaries, milestones, wickets, LBW/bowled
bonus, hauls, catches, stumpings, run-outs, captaincy) is fully scored.

## Current setup
`players.csv` is seeded from `hundered.xlsx`: **5 teams** (`sd`, `f9`, `ssw`,
`cc`, `lg`), 16 players each, one captain per team. The pipeline is pinned to
*The Hundred Men's Competition 2026* via `hundred_series.json`; delete that file
to let it auto-discover the current men's edition. Points update as each match
finishes.
