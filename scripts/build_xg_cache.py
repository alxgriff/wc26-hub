import time
import sys
from pathlib import Path

# Add the parent directory to sys.path so we can import modules
sys.path.insert(0, str(Path(__file__).resolve().parent))

import xg_api
from predict import load_ratings

def build_cache():
    print("Starting xG cache build...")
    print("This will fetch data for all teams, sleeping 6.5s between API calls to respect the 10/min rate limit.")
    
    # Load ratings will trigger the fetch_rolling_xg for all teams
    # But since predict.py does it in a loop without sleep, we'll hit the rate limit.
    # To fix this, we will pre-fetch them here with a sleep.
    
    # Let's get the list of teams from the canon
    # The easiest way is to let load_ratings run, but first we patch xg_api to sleep.
    
    original_fetch = xg_api.fetch_json
    
    def slow_fetch(url, api_key):
        print(f"Fetching {url}...")
        result = original_fetch(url, api_key)
        time.sleep(6.5)  # Sleep to stay under 10/min limit
        return result
        
    xg_api.fetch_json = slow_fetch
    
    try:
        model = load_ratings()
        print("Cache build complete! You can now run predict.py instantly.")
    except Exception as e:
        print(f"Error during cache build: {e}")

if __name__ == "__main__":
    build_cache()
