#!/usr/bin/env python3
"""
VEX V5RC World Skills Tracker — Non-Qualified Teams
====================================================
Fetches the world skills leaderboard via the Robot Events API,
filters out teams already qualified for Worlds, and generates
a static HTML page showing the top N non-qualified teams.

Usage:
    python vex_skills_tracker.py --token YOUR_API_TOKEN [--top 10] [--output index.html]

The generated HTML can be pushed to your GitHub Pages repo.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library not found. Install it with:")
    print("  pip install requests")
    sys.exit(1)

# ─── Constants ──────────────────────────────────────────────────────────────────
BASE_URL = "https://www.robotevents.com/api/v2"
V5RC_PROGRAM_ID = 1          # VEX V5 Robotics Competition
GRADE_FILTER = "High School"
PER_PAGE = 250                # Max allowed by the API
RATE_LIMIT_DELAY = 0.35       # Seconds between requests (stay under 3/sec)
CACHE_DIR = Path("cache")
MAX_RETRIES = 3

# ─── API Client ─────────────────────────────────────────────────────────────────
class RobotEventsAPI:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        self.request_count = 0

    def _get(self, endpoint: str, params: dict = None) -> dict:
        """Make a rate-limited GET request with retries."""
        url = f"{BASE_URL}{endpoint}"
        for attempt in range(MAX_RETRIES):
            time.sleep(RATE_LIMIT_DELAY)
            self.request_count += 1
            try:
                resp = self.session.get(url, params=params, timeout=30)
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 30))
                    print(f"  ⏳ Rate limited — waiting {wait}s...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.RequestException as e:
                if attempt < MAX_RETRIES - 1:
                    print(f"  ⚠ Request failed (attempt {attempt+1}): {e}")
                    time.sleep(2 ** attempt)
                else:
                    raise
        return {}

    def _get_all_pages(self, endpoint: str, params: dict = None,
                       label: str = "") -> list:
        """Paginate through all results for an endpoint."""
        if params is None:
            params = {}
        params["per_page"] = PER_PAGE
        params["page"] = 1

        all_data = []
        while True:
            result = self._get(endpoint, params)
            data = result.get("data", [])
            meta = result.get("meta", {})
            all_data.extend(data)

            current = meta.get("current_page", 1)
            last = meta.get("last_page", 1)
            total = meta.get("total", len(all_data))

            if label:
                print(f"\r  📄 {label}: page {current}/{last} "
                      f"({len(all_data)}/{total} items)", end="", flush=True)

            if current >= last:
                break
            params["page"] = current + 1

        if label:
            print()  # newline after progress
        return all_data

    def get_active_season(self) -> dict:
        """Find the current active V5RC season."""
        data = self._get("/seasons", {
            "program[]": V5RC_PROGRAM_ID,
            "active": "true",
        })
        seasons = data.get("data", [])
        if not seasons:
            raise RuntimeError("No active V5RC season found!")
        # Return the most recent active season
        return seasons[-1]

    def get_season_events(self, season_id: int) -> list:
        """Get all events for a season."""
        return self._get_all_pages("/events", {
            "season[]": season_id,
        }, label="Fetching events")

    def get_event_skills(self, event_id: int) -> list:
        """Get all skills runs for an event."""
        return self._get_all_pages(f"/events/{event_id}/skills")

    def get_event_teams(self, event_id: int, grade: str = None) -> list:
        """Get teams registered for an event."""
        params = {}
        if grade:
            params["grade[]"] = grade
        return self._get_all_pages(f"/events/{event_id}/teams", params,
                                   label="Fetching Worlds teams")

    def get_worlds_event(self, season_id: int) -> dict | None:
        """Find the World Championship event for this season."""
        data = self._get("/events", {
            "season[]": season_id,
            "level[]": "World",
            "per_page": 10,
        })
        events = data.get("data", [])
        # Look for the V5RC Worlds event
        for event in events:
            prog = event.get("program", {})
            if prog.get("id") == V5RC_PROGRAM_ID:
                return event
        # If no exact match, return any Worlds event for the season
        return events[0] if events else None


# ─── Skills Aggregation ─────────────────────────────────────────────────────────
def aggregate_skills(all_skills: list) -> list:
    """
    Aggregate skills runs into the world skills leaderboard.

    VEX rule: A team's Robot Skills score = highest (driver + programming)
    from the SAME event. We need to find the best combined score per team
    where both components are from the same event.
    """
    # Group by (team_id, event_id)
    team_event = {}
    for run in all_skills:
        team_info = run.get("team", {})
        event_info = run.get("event", {})
        team_id = team_info.get("id")
        event_id = event_info.get("id")
        skill_type = run.get("type", "")
        score = run.get("score", 0)

        key = (team_id, event_id)
        if key not in team_event:
            team_event[key] = {
                "team_id": team_id,
                "team_number": team_info.get("name", "???"),
                "event_id": event_id,
                "event_name": event_info.get("name", ""),
                "driver": 0,
                "programming": 0,
            }

        # Keep the highest score per type at this event
        if skill_type == "driver":
            team_event[key]["driver"] = max(team_event[key]["driver"], score)
        elif skill_type == "programming":
            team_event[key]["programming"] = max(
                team_event[key]["programming"], score)

    # For each team, find their best combined score across events
    best_per_team = {}
    for key, data in team_event.items():
        team_id = data["team_id"]
        combined = data["driver"] + data["programming"]

        if (team_id not in best_per_team or
                combined > best_per_team[team_id]["combined"]):
            best_per_team[team_id] = {
                "team_id": team_id,
                "team_number": data["team_number"],
                "event_name": data["event_name"],
                "driver": data["driver"],
                "programming": data["programming"],
                "combined": combined,
            }

    # Sort by combined score descending
    leaderboard = sorted(best_per_team.values(),
                         key=lambda x: (-x["combined"],
                                        -x["programming"]))
    # Assign ranks
    for i, entry in enumerate(leaderboard):
        entry["rank"] = i + 1

    return leaderboard


# ─── Caching ─────────────────────────────────────────────────────────────────────
def save_cache(filename: str, data):
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / filename
    with open(path, "w") as f:
        json.dump(data, f)
    print(f"  💾 Cached → {path}")


def load_cache(filename: str, max_age_hours: int = 6):
    path = CACHE_DIR / filename
    if not path.exists():
        return None
    age = time.time() - path.stat().st_mtime
    if age > max_age_hours * 3600:
        return None
    with open(path) as f:
        data = json.load(f)
    age_str = f"{age/3600:.1f}h"
    print(f"  📦 Using cached data ({age_str} old): {path}")
    return data


# ─── HTML Generation ────────────────────────────────────────────────────────────
def generate_html(non_qualified: list, season_name: str, top_n: int,
                  total_teams: int, worlds_qualified_count: int,
                  generated_at: str) -> str:
    """Generate a polished static HTML page for GitHub Pages."""

    rows_html = ""
    for entry in non_qualified[:top_n]:
        rows_html += f"""
            <tr>
                <td class="rank-cell">{entry['rank']}</td>
                <td class="team-cell">
                    <span class="team-number">{entry['team_number']}</span>
                </td>
                <td class="score-cell combined">{entry['combined']}</td>
                <td class="score-cell">{entry['driver']}</td>
                <td class="score-cell">{entry['programming']}</td>
                <td class="event-cell">{entry['event_name']}</td>
            </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>VEX V5RC Skills — Top Non-Qualified Teams</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=DM+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-primary: #0d1117;
            --bg-card: #161b22;
            --bg-row-hover: #1c2333;
            --border: #30363d;
            --text-primary: #e6edf3;
            --text-secondary: #8b949e;
            --text-muted: #6e7681;
            --accent-green: #3fb950;
            --accent-blue: #58a6ff;
            --accent-orange: #d29922;
            --accent-red: #f85149;
            --accent-purple: #bc8cff;
            --rank-gold: #ffd700;
            --rank-silver: #c0c0c0;
            --rank-bronze: #cd7f32;
        }}

        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            line-height: 1.5;
        }}

        .container {{
            max-width: 1100px;
            margin: 0 auto;
            padding: 2rem 1.5rem;
        }}

        /* ── Header ── */
        .header {{
            text-align: center;
            margin-bottom: 2.5rem;
            padding-bottom: 2rem;
            border-bottom: 1px solid var(--border);
        }}

        .header-badge {{
            display: inline-block;
            background: linear-gradient(135deg, #f8514922, #d2992222);
            border: 1px solid #f8514944;
            border-radius: 100px;
            padding: 0.3rem 1rem;
            font-size: 0.8rem;
            font-weight: 600;
            color: var(--accent-orange);
            text-transform: uppercase;
            letter-spacing: 0.08em;
            margin-bottom: 1rem;
        }}

        .header h1 {{
            font-size: clamp(1.6rem, 4vw, 2.4rem);
            font-weight: 700;
            margin-bottom: 0.5rem;
            background: linear-gradient(135deg, var(--text-primary), var(--accent-blue));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }}

        .header .subtitle {{
            color: var(--text-secondary);
            font-size: 1rem;
        }}

        .header .season-tag {{
            display: inline-block;
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 0.2rem 0.7rem;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.85rem;
            color: var(--accent-blue);
            margin-top: 0.8rem;
        }}

        /* ── Stats Row ── */
        .stats-row {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 1rem;
            margin-bottom: 2rem;
        }}

        .stat-card {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 1.2rem;
            text-align: center;
        }}

        .stat-card .stat-value {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 1.8rem;
            font-weight: 700;
            color: var(--accent-blue);
        }}

        .stat-card .stat-label {{
            font-size: 0.8rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-top: 0.3rem;
        }}

        .stat-card.highlight .stat-value {{
            color: var(--accent-green);
        }}

        .stat-card.warn .stat-value {{
            color: var(--accent-orange);
        }}

        /* ── Table ── */
        .table-wrapper {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 12px;
            overflow: hidden;
        }}

        .table-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 1rem 1.5rem;
            border-bottom: 1px solid var(--border);
        }}

        .table-header h2 {{
            font-size: 1.1rem;
            font-weight: 600;
        }}

        .table-header .info {{
            font-size: 0.8rem;
            color: var(--text-muted);
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
        }}

        thead th {{
            padding: 0.8rem 1rem;
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color: var(--text-muted);
            text-align: left;
            border-bottom: 1px solid var(--border);
            background: #0d111788;
        }}

        thead th.score-col {{
            text-align: right;
        }}

        tbody tr {{
            border-bottom: 1px solid var(--border);
            transition: background 0.15s ease;
        }}

        tbody tr:last-child {{
            border-bottom: none;
        }}

        tbody tr:hover {{
            background: var(--bg-row-hover);
        }}

        td {{
            padding: 0.85rem 1rem;
            font-size: 0.92rem;
        }}

        .rank-cell {{
            font-family: 'JetBrains Mono', monospace;
            font-weight: 700;
            font-size: 1rem;
            width: 50px;
            color: var(--text-muted);
        }}

        tr:nth-child(1) .rank-cell {{ color: var(--rank-gold); }}
        tr:nth-child(2) .rank-cell {{ color: var(--rank-silver); }}
        tr:nth-child(3) .rank-cell {{ color: var(--rank-bronze); }}

        .team-cell {{
            min-width: 100px;
        }}

        .team-number {{
            font-family: 'JetBrains Mono', monospace;
            font-weight: 700;
            font-size: 1rem;
            color: var(--accent-blue);
        }}

        .score-cell {{
            font-family: 'JetBrains Mono', monospace;
            text-align: right;
            min-width: 70px;
        }}

        .score-cell.combined {{
            font-weight: 700;
            font-size: 1.05rem;
            color: var(--accent-green);
        }}

        .event-cell {{
            color: var(--text-secondary);
            font-size: 0.85rem;
            max-width: 280px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}

        /* ── Footer ── */
        .footer {{
            text-align: center;
            margin-top: 2rem;
            padding-top: 1.5rem;
            border-top: 1px solid var(--border);
            color: var(--text-muted);
            font-size: 0.8rem;
        }}

        .footer a {{
            color: var(--accent-blue);
            text-decoration: none;
        }}

        .footer a:hover {{
            text-decoration: underline;
        }}

        .footer .timestamp {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.75rem;
            margin-top: 0.4rem;
        }}

        /* ── Responsive ── */
        @media (max-width: 768px) {{
            .container {{
                padding: 1rem;
            }}

            .event-cell {{
                display: none;
            }}

            td, th {{
                padding: 0.7rem 0.6rem;
            }}
        }}

        /* ── Bubble cutoff line ── */
        .cutoff-note {{
            text-align: center;
            padding: 0.8rem;
            background: linear-gradient(135deg, #f8514911, #d2992211);
            border-top: 1px dashed var(--accent-orange);
            border-bottom: 1px dashed var(--accent-orange);
            color: var(--accent-orange);
            font-size: 0.82rem;
            font-weight: 500;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header class="header">
            <div class="header-badge">🤖 Skills Tracker</div>
            <h1>Top Non-Qualified Teams</h1>
            <p class="subtitle">
                V5RC High School — World Skills Leaderboard
            </p>
            <span class="season-tag">{season_name}</span>
        </header>

        <div class="stats-row">
            <div class="stat-card">
                <div class="stat-value">{total_teams:,}</div>
                <div class="stat-label">Teams with Skills</div>
            </div>
            <div class="stat-card highlight">
                <div class="stat-value">{worlds_qualified_count:,}</div>
                <div class="stat-label">Worlds Qualified</div>
            </div>
            <div class="stat-card warn">
                <div class="stat-value">{non_qualified[0]['combined'] if non_qualified else '—'}</div>
                <div class="stat-label">Bubble Score</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{top_n}</div>
                <div class="stat-label">Shown Below</div>
            </div>
        </div>

        <div class="table-wrapper">
            <div class="table-header">
                <h2>Teams on the Bubble</h2>
                <span class="info">
                    Highest-ranked teams NOT yet qualified for Worlds
                </span>
            </div>

            <div class="cutoff-note">
                ⚡ These teams are closest to the skills qualification cutoff
            </div>

            <table>
                <thead>
                    <tr>
                        <th>#</th>
                        <th>Team</th>
                        <th class="score-col">Combined</th>
                        <th class="score-col">Driver</th>
                        <th class="score-col">Prog</th>
                        <th>Best Event</th>
                    </tr>
                </thead>
                <tbody>{rows_html}
                </tbody>
            </table>
        </div>

        <footer class="footer">
            <p>
                Data from
                <a href="https://www.robotevents.com" target="_blank">
                    RobotEvents.com
                </a>
                via the official API ·
                <a href="https://www.robotevents.com/robot-competitions/vex-robotics-competition/standings/skills"
                   target="_blank">
                    Full Standings ↗
                </a>
            </p>
            <p class="timestamp">Generated: {generated_at}</p>
        </footer>
    </div>
</body>
</html>"""
    return html


# ─── Main Workflow ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="VEX V5RC Skills Tracker — Non-Qualified Teams")
    parser.add_argument("--token", required=True,
                        help="Robot Events API bearer token")
    parser.add_argument("--top", type=int, default=10,
                        help="Number of non-qualified teams to show (default: 10)")
    parser.add_argument("--output", default="index.html",
                        help="Output HTML filename (default: index.html)")
    parser.add_argument("--cache-hours", type=int, default=6,
                        help="Cache validity in hours (default: 6)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Ignore cached data and fetch fresh")
    args = parser.parse_args()

    api = RobotEventsAPI(args.token)
    cache_hours = 0 if args.no_cache else args.cache_hours

    print("=" * 60)
    print("  VEX V5RC Skills Tracker — Non-Qualified Teams")
    print("=" * 60)

    # ── Step 1: Find the current season ──────────────────────────
    print("\n🔍 Step 1: Finding current V5RC season...")
    season = api.get_active_season()
    season_id = season["id"]
    season_name = season.get("name", f"Season {season_id}")
    print(f"  ✅ {season_name} (ID: {season_id})")

    # ── Step 2: Get all events for the season ────────────────────
    print("\n📋 Step 2: Getting all season events...")
    events_cache = load_cache(f"events_{season_id}.json", cache_hours)
    if events_cache:
        events = events_cache
    else:
        events = api.get_season_events(season_id)
        save_cache(f"events_{season_id}.json", events)
    print(f"  ✅ Found {len(events)} events")

    # ── Step 3: Find Worlds event & get qualified teams ──────────
    print("\n🏆 Step 3: Finding Worlds event & qualified teams...")
    worlds_event = api.get_worlds_event(season_id)
    qualified_team_ids = set()

    if worlds_event:
        worlds_id = worlds_event["id"]
        worlds_name = worlds_event.get("name", "World Championship")
        print(f"  ✅ Found: {worlds_name} (ID: {worlds_id})")

        worlds_teams_cache = load_cache(
            f"worlds_teams_{worlds_id}.json", cache_hours)
        if worlds_teams_cache:
            worlds_teams = worlds_teams_cache
        else:
            worlds_teams = api.get_event_teams(worlds_id)
            save_cache(f"worlds_teams_{worlds_id}.json", worlds_teams)

        # Filter to only High School teams
        for team in worlds_teams:
            grade = team.get("grade", "")
            if grade == GRADE_FILTER or not grade:
                qualified_team_ids.add(team["id"])

        print(f"  ✅ {len(qualified_team_ids)} HS teams registered for Worlds")
    else:
        print("  ⚠ No Worlds event found yet — showing all teams ranked")

    # ── Step 4: Fetch skills data from all events ────────────────
    print("\n🎯 Step 4: Fetching skills data from events...")
    skills_cache = load_cache(f"skills_{season_id}.json", cache_hours)

    if skills_cache:
        all_skills = skills_cache
    else:
        all_skills = []
        skills_events = [e for e in events
                         if e.get("event_type") != "workshop"]
        total = len(skills_events)

        for i, event in enumerate(skills_events):
            eid = event["id"]
            ename = event.get("name", f"Event {eid}")
            print(f"\r  🔄 Event {i+1}/{total}: {ename[:50]:<50}",
                  end="", flush=True)
            try:
                skills = api.get_event_skills(eid)
                all_skills.extend(skills)
            except Exception as e:
                print(f"\n  ⚠ Error fetching skills for {ename}: {e}")

        print(f"\n  ✅ Collected {len(all_skills)} total skills runs")
        save_cache(f"skills_{season_id}.json", all_skills)

    print(f"  📊 Total skills runs: {len(all_skills):,}")

    # ── Step 5: Aggregate & rank ─────────────────────────────────
    print("\n📊 Step 5: Aggregating skills leaderboard...")
    leaderboard = aggregate_skills(all_skills)
    print(f"  ✅ {len(leaderboard)} teams with skills scores")

    # ── Step 6: Filter out qualified teams ───────────────────────
    print("\n🔀 Step 6: Filtering out Worlds-qualified teams...")
    non_qualified = [t for t in leaderboard
                     if t["team_id"] not in qualified_team_ids]

    # Re-rank the non-qualified list
    for i, entry in enumerate(non_qualified):
        entry["bubble_rank"] = i + 1

    qualified_in_top = len(leaderboard) - len(non_qualified)
    print(f"  ✅ {qualified_in_top} qualified teams filtered out")
    print(f"  ✅ {len(non_qualified)} non-qualified teams remaining")

    if non_qualified:
        print(f"\n  🔥 Top bubble score: {non_qualified[0]['combined']} "
              f"({non_qualified[0]['team_number']})")

    # ── Step 7: Generate HTML ────────────────────────────────────
    print(f"\n🖨  Step 7: Generating HTML ({args.output})...")
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = generate_html(
        non_qualified=non_qualified,
        season_name=season_name,
        top_n=args.top,
        total_teams=len(leaderboard),
        worlds_qualified_count=len(qualified_team_ids),
        generated_at=generated_at,
    )

    output_path = Path(args.output)
    output_path.write_text(html, encoding="utf-8")
    print(f"  ✅ Written to {output_path.resolve()}")

    # ── Summary ──────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"  API requests made: {api.request_count}")
    print(f"  Teams on leaderboard: {len(leaderboard):,}")
    print(f"  Worlds-qualified (filtered): {len(qualified_team_ids):,}")
    print(f"  Non-qualified teams: {len(non_qualified):,}")
    if non_qualified:
        top = non_qualified[:args.top]
        print(f"\n  Top {len(top)} non-qualified:")
        for t in top:
            print(f"    #{t['rank']:>4}  {t['team_number']:<10} "
                  f"{t['combined']:>4} "
                  f"(D:{t['driver']} + P:{t['programming']})")
    print(f"\n  Output: {output_path.resolve()}")
    print(f"  Push to your GitHub Pages repo and you're live! 🚀")
    print("=" * 60)


if __name__ == "__main__":
    main()
