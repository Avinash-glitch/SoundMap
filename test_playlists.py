"""
Quick test: list all playlists returned by /me/playlists.
Run: python3 test_playlists.py
Needs SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in a .env file (or env vars).
Opens a browser for one-time login, then prints every playlist with track count.
"""
import os, webbrowser, urllib.parse, http.server, threading, requests, json
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID     = os.environ["SPOTIFY_CLIENT_ID"]
CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
REDIRECT_URI  = "http://127.0.0.1:9753/callback"
SCOPES        = "playlist-read-private playlist-read-collaborative"

# --- tiny one-shot callback server ---
_code = None

class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global _code
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _code = qs.get("code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<h2>Got it! You can close this tab.</h2>")
    def log_message(self, *_): pass  # silence request logs

def _get_token():
    # 1. open browser for auth
    params = urllib.parse.urlencode({
        "client_id": CLIENT_ID, "response_type": "code",
        "redirect_uri": REDIRECT_URI, "scope": SCOPES,
    })
    webbrowser.open(f"https://accounts.spotify.com/authorize?{params}")

    # 2. spin up callback server
    server = http.server.HTTPServer(("127.0.0.1", 9753), _Handler)
    t = threading.Thread(target=server.handle_request)
    t.start(); t.join(timeout=120)

    if not _code:
        raise RuntimeError("No auth code received — did the browser open?")

    # 3. exchange code for token
    resp = requests.post("https://accounts.spotify.com/api/token", data={
        "grant_type": "authorization_code", "code": _code,
        "redirect_uri": REDIRECT_URI,
    }, auth=(CLIENT_ID, CLIENT_SECRET))
    resp.raise_for_status()
    return resp.json()["access_token"]

def fetch_all_playlists(token):
    """Fetch all playlists from the user's account with pagination."""
    headers = {"Authorization": f"Bearer {token}"}
    url = "https://api.spotify.com/v1/me/playlists"
    playlists = []
    
    while url:
        r = requests.get(url, headers=headers, params={"limit": 50} if url.endswith("playlists") else None)
        r.raise_for_status()
        body = r.json()
        playlists.extend(body.get("items", []))
        url = body.get("next")
    
    return playlists

def fetch_all_playlist_items(token, playlist_id):
    """Fetch all items from a specific playlist using the /items endpoint."""
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://api.spotify.com/v1/playlists/{playlist_id}/items"
    all_items = []
    
    while url:
        r = requests.get(url, headers=headers, params={"limit": 50} if "offset" not in url else None)
        r.raise_for_status()
        body = r.json()
        all_items.extend(body.get("items", []))
        url = body.get("next")
    
    return all_items

def get_playlist_by_name(token, playlist_name):
    """Find a playlist by its name and return its ID and items."""
    playlists = fetch_all_playlists(token)
    for pl in playlists:
        if pl.get("name") == playlist_name:
            playlist_id = pl.get("id")
            items = fetch_all_playlist_items(token, playlist_id)
            return playlist_id, items
    return None, None

def print_tracks(items, playlist_name):
    """Print all tracks from a playlist."""
    if not items:
        print(f"No tracks found in {playlist_name}")
        return
    
    print(f"\n{'='*80}")
    print(f"Playlist: {playlist_name}")
    print(f"Total items: {len(items)}")
    print(f"{'='*80}\n")
    
    track_count = 0
    
    for i, item in enumerate(items, 1):
        # Spotify returns 'track' key on /playlists/{id}/items
        track_info = item.get('track')
        
        # Skip if no track info or if it's not a track (could be episode, local file, etc.)
        if not track_info or not isinstance(track_info, dict):
            continue
        
        # Check if it's a track (has 'type' field and type is 'track')
        if track_info.get('type') != 'track':
            continue
        
        # Extract track name
        track_name = track_info.get('name', 'Unknown')
        
        # Extract artists (array of artist objects)
        artists = track_info.get('artists', [])
        if artists and isinstance(artists, list):
            artist_names = ", ".join([artist.get('name', '') for artist in artists if isinstance(artist, dict)])
        else:
            artist_names = "Unknown artist"
        
        track_count += 1
        print(f"{track_count:3}. {track_name} - {artist_names}")
    
    print(f"\n✅ Total tracks displayed: {track_count}")

if __name__ == "__main__":
    token = _get_token()
    
    print("Fetching all playlists…\n")
    playlists = fetch_all_playlists(token)
    
    print(f"{'#':<4} {'Tracks':<8} {'ID':<24} Name")
    print("-" * 70)
    for i, pl in enumerate(playlists, 1):
        name = pl.get("name", "(unnamed)")
        total = pl.get("tracks", {}).get("total", "?")
        pl_id = pl.get("id", "")
        print(f"{i:<4} {str(total):<8} {pl_id:<24} {name}")
    
    print(f"\nTotal: {len(playlists)} playlists")
    
    # Check for playlist by name
    print(f"\n{'='*80}")
    print(f"Fetching playlist by name: MorethanJazz")
    print(f"{'='*80}")
    playlist_id, items = get_playlist_by_name(token, "MorethanJazz")
    if items:
        print_tracks(items, "MorethanJazz")
    else:
        print(f"Playlist 'MorethanJazz' not found")
