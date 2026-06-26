import os
import json
import urllib.request
import urllib.parse
import ssl

CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "xg_cache.json")

def _load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}

def _save_cache(cache_data):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache_data, f, indent=2)

def get_api_key():
    key = os.environ.get("API_FOOTBALL_KEY")
    if not key:
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("API_FOOTBALL_KEY="):
                        key = line.split("=", 1)[1].strip()
    return key

def fetch_json(endpoint, api_key):
    url = f"https://v3.football.api-sports.io/{endpoint}"
    req = urllib.request.Request(url, headers={
        "x-apisports-key": api_key,
        "x-rapidapi-key": api_key  # Send both just in case they are using rapidapi url, though host matters
    })
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        
        with urllib.request.urlopen(req, context=ctx) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        print(f"API Error fetching {url}: {e}")
        return None

def fetch_rolling_xg(team_name: str) -> dict | None:
    """
    Fetch the rolling xG created and conceded for a team over their last 10 matches.
    Returns: {"xG_C": float, "xG_D": float} or None on failure (triggers fallback).
    """
    cache = _load_cache()
    if team_name in cache:
        return cache[team_name]
        
    api_key = get_api_key()
    if not api_key:
        return None
        
    # 1. Search for team ID
    name_encoded = urllib.parse.quote(team_name)
    team_data = fetch_json(f"teams?search={name_encoded}", api_key)
    
    if not team_data or not team_data.get("response"):
        cache[team_name] = None
        _save_cache(cache)
        return None
        
    # Take the first matched team
    team_id = team_data["response"][0]["team"]["id"]
    
    # 2. Get last 10 fixtures
    fixtures_data = fetch_json(f"fixtures?team={team_id}&last=10", api_key)
    
    if not fixtures_data or not fixtures_data.get("response"):
        cache[team_name] = None
        _save_cache(cache)
        return None
        
    fixtures = fixtures_data["response"]
    
    xg_created = []
    xg_conceded = []
    
    # 3. For each fixture, fetch statistics
    for f in fixtures:
        fixture_id = f["fixture"]["id"]
        stats_data = fetch_json(f"fixtures/statistics?fixture={fixture_id}", api_key)
        
        if not stats_data or not stats_data.get("response"):
            continue
            
        # The response is an array of two objects (one for each team)
        team_stats = None
        opp_stats = None
        
        for ts in stats_data["response"]:
            if ts["team"]["id"] == team_id:
                team_stats = ts["statistics"]
            else:
                opp_stats = ts["statistics"]
                
        def extract_xg(stats_list):
            if not stats_list:
                return None
            for s in stats_list:
                if s["type"] == "expected_goals" and s["value"] is not None:
                    try:
                        return float(s["value"])
                    except ValueError:
                        pass
            return None
            
        c = extract_xg(team_stats)
        d = extract_xg(opp_stats)
        
        if c is not None:
            xg_created.append(c)
        if d is not None:
            xg_conceded.append(d)
            
    if len(xg_created) > 0 and len(xg_conceded) > 0:
        avg_c = sum(xg_created) / len(xg_created)
        avg_d = sum(xg_conceded) / len(xg_conceded)
        result = {"xG_C": avg_c, "xG_D": avg_d}
    else:
        # Fallback if no xG data is found
        result = None
        
    cache[team_name] = result
    _save_cache(cache)
    return result
