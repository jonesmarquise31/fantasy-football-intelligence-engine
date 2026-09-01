import os
import re
import sys
import json
from pathlib import Path

import pandas as pd
import webview
from dotenv import load_dotenv
from espn_api.football import League
from playwright.sync_api import sync_playwright


# ─────────────────────────────────────────────────────────────────────────────
# Credential storage
# ─────────────────────────────────────────────────────────────────────────────
# A frozen .app does not run with its own directory as the working directory —
# macOS launches it with CWD set to "/". Writing a relative ".env" therefore
# lands somewhere unpredictable and is not found on the next launch, so the
# user is asked to authenticate again every time.
#
# Running from source keeps the .env beside the project, which is what the
# developer workflow and .gitignore already expect.

APP_SUPPORT = Path.home() / "Library" / "Application Support" / "Underwriter"


def env_path() -> Path:
    """Where credentials live, for this run mode."""
    if getattr(sys, "frozen", False):
        APP_SUPPORT.mkdir(parents=True, exist_ok=True)
        return APP_SUPPORT / ".env"
    return Path(__file__).resolve().parent / ".env"


def write_env(updates: dict) -> Path:
    """Merge keys into the .env, preserving any the app does not manage.

    Written 0600: the file holds a live ESPN session token, and anything less
    leaves it world-readable on a shared machine.
    """
    path = env_path()
    existing = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            existing[key.strip()] = value.strip()

    existing.update({k: v for k, v in updates.items() if v not in (None, "")})

    path.write_text("".join(f"{k}={v}\n" for k, v in existing.items()))
    path.chmod(0o600)
    return path


load_dotenv(env_path())

def parse_owner(owner):
    if isinstance(owner, dict):
        first = owner.get('firstName', '').strip()
        last = owner.get('lastName', '').strip()
        if first or last:
            return f"{first} {last}".strip()
        return owner.get('displayName', 'Unknown')
    elif hasattr(owner, 'firstName') or hasattr(owner, 'lastName'):
        first = getattr(owner, 'firstName', '') or ''
        last = getattr(owner, 'lastName', '') or ''
        if first or last:
            return f"{first} {last}".strip()
    return str(owner) if owner else "Unassigned"

class DesktopBridge:
    def trigger_login(self):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=False, channel="chrome")
                context = browser.new_context()
                page = context.new_page()
                page.goto("https://fantasy.espn.com/", timeout=60000)
                try:
                    page.wait_for_event("close", timeout=300000)
                except Exception:
                    pass
                cookies = context.cookies("https://fantasy.espn.com")
                swid = next((c['value'] for c in cookies if c['name'] == 'SWID'), None)
                espn_s2 = next((c['value'] for c in cookies if c['name'] == 'espn_s2'), None)
                try:
                    browser.close()
                except Exception:
                    pass
                if swid and espn_s2:
                    write_env({
                        "SWID": swid,
                        "ESPN_S2": espn_s2,
                        "LEAGUE_ID": os.getenv("LEAGUE_ID", ""),
                    })
                    return {"success": True, "message": "Authentication cookies captured successfully."}
                return {"success": False, "message": "Could not find session cookies."}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def save_league_from_url(self, url_or_id):
        load_dotenv(env_path(), override=True)
        swid = os.getenv("SWID")
        espn_s2 = os.getenv("ESPN_S2")
        if not swid or not espn_s2:
            return {"success": False, "message": "Session cookies missing. Authenticate first."}
        try:
            match = re.search(r'leagueId=(\d+)', str(url_or_id))
            if not match:
                match = re.search(r'(\d+)', str(url_or_id))
            if not match:
                return {"success": False, "message": "Could not extract a valid League ID."}
            league_id = int(match.group(1))
            league = League(league_id=league_id, year=2026, swid=swid, espn_s2=espn_s2)
            write_env({"SWID": swid, "ESPN_S2": espn_s2, "LEAGUE_ID": league_id})
            teams = []
            for t in league.teams:
                owner_name = parse_owner(getattr(t, 'owner', None))
                teams.append({"name": getattr(t, 'team_name', 'Unknown Team'), "owner": owner_name})
            return {
                "success": True,
                "league_name": getattr(league.settings, 'name', 'Fantasy League'),
                "teams": teams,
                "league_id": league_id
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_league_protocol(self):
        load_dotenv(env_path(), override=True)
        swid = os.getenv("SWID")
        espn_s2 = os.getenv("ESPN_S2")
        league_id = os.getenv("LEAGUE_ID")
        
        if not swid or not espn_s2 or not league_id:
            return {"success": False, "message": "Missing authentication or league ID. Sync your league first."}
            
        try:
            league = League(league_id=int(league_id), year=2026, swid=swid, espn_s2=espn_s2)
            settings = league.settings
            league_name = getattr(settings, 'name', 'Fantasy League')
            team_count = len(league.teams)
            reg_season = getattr(settings, 'reg_season_count', 14)
            
            protocol_text = (
                f"LEAGUE PROTOCOL // {league_name.upper()}\n"
                f"==================================================\n"
                f"• League Scale: {team_count}-Team Competitive Field\n"
                f"• Regular Season Structure: {reg_season} Weeks\n"
                f"• Scoring Foundation: Standard / PPR Format Ruleset\n\n"
                f"TACTICAL DRAFT INCENTIVES & STRATEGY:\n"
                f"--------------------------------------------------\n"
                f"1. Positional Scarcity (RB/WR Tiers): In a {team_count}-team field, starting running back depth drops off drastically by round 4. Secure anchor volume early, but capitalize on elite wide receiver tier breaks.\n"
                f"2. Value Over Replacement: Target elite tier assets in early rounds to maximize weekly positional advantage over league opponents.\n"
                f"3. Schedule Architecture: Plan roster construction around multi-week playoff resilience and bye-week mitigation to maintain weekly floor dominance."
            )
            return {"success": True, "output": protocol_text}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def generate_predraft_board(self):
        load_dotenv(env_path(), override=True)
        swid = os.getenv("SWID")
        espn_s2 = os.getenv("ESPN_S2")
        league_id = os.getenv("LEAGUE_ID")
        
        if not swid or not espn_s2 or not league_id:
            return {"success": False, "message": "Missing authentication or league ID. Sync your league first."}
            
        try:
            league = League(league_id=int(league_id), year=2026, swid=swid, espn_s2=espn_s2)
            team_count = len(league.teams)
            players = league.free_agents(size=350)
            pool_data = []
            for p in players:
                pool_data.append({
                    'player_id': p.playerId,
                    'name': p.name,
                    'position': p.position,
                    'pro_team': p.proTeam,
                    'projected_points': getattr(p, 'projected_total_points', 0.0)
                })
            
            df = pd.DataFrame(pool_data)
            qbs = df[df['position'] == 'QB'].sort_values(by='projected_points', ascending=False).head(5)
            rbs = df[df['position'] == 'RB'].sort_values(by='projected_points', ascending=False).head(10)
            wrs = df[df['position'] == 'WR'].sort_values(by='projected_points', ascending=False).head(10)
            tes = df[df['position'] == 'TE'].sort_values(by='projected_points', ascending=False).head(5)
            
            board_text = (
                f"PRE-DRAFT INTELLIGENCE BOARD & TIER MAPPING\n"
                f"==================================================\n\n"
                f"ROUND-BY-ROUND DRAFT BLUEPRINT ({team_count}-TEAM FIELD):\n"
                f"--------------------------------------------------\n"
                f"• Rounds 1 - 2 (Anchor Tier): Target elite Tier-1 RBs or alpha WRs. In a {team_count}-team league, securing high-volume workload assets here dictates your weekly floor.\n"
                f"• Rounds 3 - 4 (Scarcity Mitigation): Lock down your second starter or flex anchor. Watch positional runs on running backs before the cliff drops off.\n"
                f"• Rounds 5 - 8 (Value Accumulation & Upside): Target high-upside WR breakouts and dual-threat QBs if elite tier values slip past ADP.\n"
                f"• Rounds 9+ (Depth & Handcuffs): Secure high-ceiling injury handcuffs and rookie sleepers with league-winning upside.\n\n"
                f"TOP PRE-DRAFT TARGETS BY POSITION:\n"
                f"--------------------------------------------------\n\n"
                f"[QUARTERBACKS (QB) - Tier 1 Targets]\n"
            )
            for _, row in qbs.iterrows():
                board_text += f" - {row['name']} ({row['pro_team']}) | Proj: {row['projected_points']:.1f} pts\n"
                
            board_text += f"\n[RUNNING BACKS (RB) - Anchor & Workhorse Tiers]\n"
            for _, row in rbs.iterrows():
                board_text += f" - {row['name']} ({row['pro_team']}) | Proj: {row['projected_points']:.1f} pts\n"
                
            board_text += f"\n[WIDE RECEIVERS (WR) - Volume & Explosive Tiers]\n"
            for _, row in wrs.iterrows():
                board_text += f" - {row['name']} ({row['pro_team']}) | Proj: {row['projected_points']:.1f} pts\n"
                
            board_text += f"\n[TIGHT ENDS (TE) - Positional Advantage]\n"
            for _, row in tes.iterrows():
                board_text += f" - {row['name']} ({row['pro_team']}) | Proj: {row['projected_points']:.1f} pts\n"
                
            return {"success": True, "output": board_text}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_live_draft_advice(self):
        load_dotenv(env_path(), override=True)
        swid = os.getenv("SWID")
        espn_s2 = os.getenv("ESPN_S2")
        league_id = os.getenv("LEAGUE_ID")
        
        if not swid or not espn_s2 or not league_id:
            return {"success": False, "message": "Missing authentication or league ID. Sync your league first."}
            
        try:
            league = League(league_id=int(league_id), year=2026, swid=swid, espn_s2=espn_s2)
            team_count = len(league.teams)
            players = league.free_agents(size=250)
            pool_data = []
            for p in players:
                pool_data.append({
                    'name': p.name,
                    'position': p.position,
                    'pro_team': p.proTeam,
                    'projected_points': getattr(p, 'projected_total_points', 0.0)
                })
            
            df = pd.DataFrame(pool_data)
            if df.empty:
                return {"success": False, "message": "Could not retrieve available player pool."}
                
            top_qbs = df[df['position'] == 'QB'].sort_values(by='projected_points', ascending=False).head(3)
            top_rbs = df[df['position'] == 'RB'].sort_values(by='projected_points', ascending=False).head(5)
            top_wrs = df[df['position'] == 'WR'].sort_values(by='projected_points', ascending=False).head(5)
            top_tes = df[df['position'] == 'TE'].sort_values(by='projected_points', ascending=False).head(3)
            
            advice_text = (
                f"REAL-TIME DRAFT ADVISOR // ON-THE-CLOCK TICKER\n"
                f"==================================================\n"
                f"Status: Live Board Scan Active ({team_count}-Team Field)\n"
                f"Directive: Best unrostered value currently available on the board.\n\n"
            )
            
            advice_text += "[BEST AVAILABLE RUNNING BACKS (RB) - Priority Value]\n"
            for _, r in top_rbs.iterrows():
                advice_text += f" • {r['name']} ({r['pro_team']}) | Proj: {r['projected_points']:.1f} pts\n"
                
            advice_text += "\n[BEST AVAILABLE WIDE RECEIVERS (WR) - High Floor / Upside]\n"
            for _, r in top_wrs.iterrows():
                advice_text += f" • {r['name']} ({r['pro_team']}) | Proj: {r['projected_points']:.1f} pts\n"
                
            advice_text += "\n[BEST AVAILABLE QUARTERBACKS (QB) - Tier Control]\n"
            for _, r in top_qbs.iterrows():
                advice_text += f" • {r['name']} ({r['pro_team']}) | Proj: {r['projected_points']:.1f} pts\n"
                
            advice_text += "\n[BEST AVAILABLE TIGHT ENDS (TE) - Positional Advantage]\n"
            for _, r in top_tes.iterrows():
                advice_text += f" • {r['name']} ({r['pro_team']}) | Proj: {r['projected_points']:.1f} pts\n"
                
            advice_text += (
                f"\n--------------------------------------------------\n"
                f"TACTICAL TIP: Hit this button right before your pick to verify who's still sitting on the board before pulling the trigger."
            )
            return {"success": True, "output": advice_text}
        except Exception as e:
            return {"success": False, "message": str(e)}

def get_html_ui():
    load_dotenv(env_path(), override=True)
    authenticated = bool(os.getenv("SWID") and os.getenv("ESPN_S2"))
    league_id = os.getenv("LEAGUE_ID", "")
    
    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Underwriter // Intelligence Terminal</title>
    <style>
        :root {{
            --bg-deep: #101A2B;
            --accent-brass: #C9A24B;
            --text-paper: #EDE7DA;
            --panel-bg: #18263D;
            --border-subtle: #2A3B58;
        }}
        body {{
            background-color: var(--bg-deep);
            color: var(--text-paper);
            font-family: 'JetBrains Mono', monospace;
            margin: 0;
            padding: 30px;
        }}
        h1 {{
            color: var(--accent-brass);
            font-size: 1.5rem;
            text-transform: uppercase;
            letter-spacing: 2px;
            border-bottom: 1px solid var(--border-subtle);
            padding-bottom: 10px;
        }}
        .card {{
            background: var(--panel-bg);
            border: 1px solid var(--border-subtle);
            padding: 20px;
            margin-bottom: 20px;
            border-radius: 4px;
        }}
        input, button {{
            background: var(--bg-deep);
            color: var(--text-paper);
            border: 1px solid var(--accent-brass);
            padding: 10px 15px;
            font-family: inherit;
            margin-right: 10px;
            margin-top: 5px;
        }}
        input {{ width: 350px; cursor: text; }}
        button {{ cursor: pointer; font-weight: bold; }}
        button:hover {{ background: var(--accent-brass); color: var(--bg-deep); }}
        #output {{
            white-space: pre-wrap;
            background: #0B121D;
            padding: 15px;
            border: 1px solid var(--border-subtle);
            margin-top: 15px;
            font-size: 0.85rem;
            color: #A8B2C1;
            max-height: 450px;
            overflow-y: auto;
        }}
        .hidden {{ display: none; }}
        .nav-actions {{ margin-bottom: 15px; display: flex; gap: 10px; flex-wrap: wrap; }}
    </style>
</head>
<body>
    <h1>Underwriter // Intelligence Terminal v1</h1>
    
    <div class="nav-actions">
        <button onclick="fetchProtocol()">1. Protocol</button>
        <button onclick="generatePredraftBoard()">2. Pre-Draft Board</button>
        <button onclick="fetchLiveAdvice()">3. On-The-Clock Advisor</button>
    </div>

    <div id="authCard" class="card {"hidden" if authenticated else ""}">
        <h3>Step 1: ESPN Authentication</h3>
        <p>Click below to sign in. Once logged into ESPN, close the browser window to proceed.</p>
        <button onclick="startLogin()">Connect ESPN Account</button>
    </div>

    <div id="leagueCard" class="card {"hidden" if not authenticated else ""}">
        <h3>Step 2: League Configuration</h3>
        <p>Paste your ESPN Fantasy League URL or ID:</p>
        <input type="text" id="leagueInput" value="{league_id}" placeholder="https://fantasy.espn.com/football/league?leagueId=...">
        <button onclick="submitLeague()">Sync League</button>
    </div>

    <div class="card">
        <h3>System Telemetry</h3>
        <div id="output">{"Status: Authenticated. Use the navigation buttons above to load Protocol, Pre-Draft Board, or On-The-Clock Advisor." if authenticated else "Status: Not authenticated. Click 'Connect ESPN Account' above."}</div>
    </div>

    <script>
        async function startLogin() {{
            document.getElementById('output').innerText = "Browser launched. Sign into ESPN, then close the browser window when done...";
            let res = await window.pywebview.api.trigger_login();
            if(res.success) {{
                document.getElementById('authCard').classList.add('hidden');
                document.getElementById('leagueCard').classList.remove('hidden');
                document.getElementById('output').innerText = "Authentication saved! Now paste your league URL above.";
            }} else {{
                document.getElementById('output').innerText = `Error: ${{res.message}}`;
            }}
        }}

        async function submitLeague() {{
            let val = document.getElementById('leagueInput').value;
            if(!val) {{
                alert("Please paste your league URL or ID");
                return;
            }}
            document.getElementById('output').innerText = "Syncing league telemetry and saving configuration...";
            let res = await window.pywebview.api.save_league_from_url(val);
            if(res.success) {{
                document.getElementById('authCard').classList.add('hidden');
                document.getElementById('leagueCard').classList.add('hidden');
                document.getElementById('output').innerText = `CONNECTED: ${{res.league_name}}\\nLeague ID: ${{res.league_id}}\\nTeams Loaded: ${{res.teams.length}}\\n\\nTeams:\\n` + res.teams.map(t => `- ${{t.name}} (${{t.owner}})`).join('\\n');
            }} else {{
                document.getElementById('output').innerText = `Error: ${{res.message}}`;
            }}
        }}

        async function fetchProtocol() {{
            document.getElementById('output').innerText = "Querying league settings and building strategic incentive analysis...";
            let res = await window.pywebview.api.get_league_protocol();
            if(res.success) {{
                document.getElementById('output').innerText = res.output;
            }} else {{
                document.getElementById('output').innerText = `Error: ${{res.message}}`;
            }}
        }}

        async function generatePredraftBoard() {{
            document.getElementById('output').innerText = "Analyzing pre-draft player pool, positioning tiers, and round-by-round blueprint...";
            let res = await window.pywebview.api.generate_predraft_board();
            if(res.success) {{
                document.getElementById('output').innerText = res.output;
            }} else {{
                document.getElementById('output').innerText = `Error: ${{res.message}}`;
            }}
        }}

        async function fetchLiveAdvice() {{
            document.getElementById('output').innerText = "Scanning live draft board for unrostered player values...";
            let res = await window.pywebview.api.get_live_draft_advice();
            if(res.success) {{
                document.getElementById('output').innerText = res.output;
            }} else {{
                document.getElementById('output').innerText = `Error: ${{res.message}}`;
            }}
        }}
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    api = DesktopBridge()
    window = webview.create_window('Underwriter', html=get_html_ui(), js_api=api, width=950, height=850, background_color='#101A2B')
    webview.start()
