"""
Quick test: read 60 tracks from the already-generated map data,
ask GPT to sort into 3 mood playlists, create them in Spotify.

Usage:
  1. Make sure the server has processed your library (user_data/{user_id}.json exists)
  2. Log in at http://127.0.0.1:8000, then visit http://127.0.0.1:8000/debug/token
  3. python test_moods.py <your_access_token> <your_user_id>
"""

import sys
import json
import os
import glob
import requests
import anthropic
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

# ── Args ──────────────────────────────────────────────────────────────────
SPOTIFY_TOKEN = sys.argv[1] if len(sys.argv) > 1 else input("Spotify access token: ").strip()
USER_ID       = sys.argv[2] if len(sys.argv) > 2 else input("Spotify user ID: ").strip()

h = {"Authorization": f"Bearer {SPOTIFY_TOKEN}", "Content-Type": "application/json"}

# ── 1. Load map data from disk ────────────────────────────────────────────
map_file = os.path.join("user_data", f"{USER_ID}.json")
if not os.path.exists(map_file):
    # Try any file in user_data/
    files = glob.glob("user_data/*.json")
    if not files:
        sys.exit("No map data found. Process your library first by logging in.")
    map_file = files[0]
    print(f"Using {map_file}")

with open(map_file) as f:
    map_data = json.load(f)

all_points = map_data.get("points", [])
print(f"Map has {len(all_points)} tracks total")

# Pick 60 tracks — prefer ones with playlist tags for richer context
with_playlist = [p for p in all_points if p.get("playlists")]
without       = [p for p in all_points if not p.get("playlists")]
sample = (with_playlist + without)[:60]

print(f"Using {len(sample)} tracks for test ({len([p for p in sample if p.get('playlists')])} have playlist tags)\n")

# ── 2. Ask Claude to split into 3 mood playlists ─────────────────────────
print("Asking Claude to group into 3 mood playlists…")
client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

track_lines = []
for p in sample:
    label = f'{p["name"]} — {p["artist"]}'
    if p.get("playlists"):
        label += f' [from: {", ".join(p["playlists"][:2])}]'
    track_lines.append(f"- {label}")

track_list = "\n".join(track_lines)

resp_claude = client.messages.create(
    model="claude-haiku-4-5",
    max_tokens=1500,
    system=(
        "You are a music curator. Group the given tracks into exactly 3 mood-based playlists. "
        "Give each playlist a short evocative name (2–3 words). "
        "Assign every track to exactly one playlist based on vibe and mood. "
        "Use only the exact track name as it appears after '- ' and before ' —' in your response. "
        "Respond ONLY with valid JSON, no markdown:\n"
        '{"playlists": [{"name": "...", "tracks": ["exact track name", ...]}, ...]}'
    ),
    messages=[{"role": "user", "content": f"Tracks:\n{track_list}"}],
)

raw = resp_claude.content[0].text.strip()
if raw.startswith("```"):
    raw = "\n".join(raw.split("\n")[1:]).rsplit("```", 1)[0]

data = json.loads(raw)
playlists = data["playlists"]

print("\nClaude grouped tracks into:")
for pl in playlists:
    print(f"  🎵 {pl['name']} ({len(pl['tracks'])} tracks)")
    for t in pl["tracks"][:4]:
        print(f"      {t}")
    if len(pl["tracks"]) > 4:
        print(f"      … and {len(pl['tracks'])-4} more")

# ── 3. Match GPT track names back to Spotify IDs ─────────────────────────
name_to_id = {p["name"].lower(): p["id"] for p in sample}

# ── 4. Create playlists in Spotify ───────────────────────────────────────
print(f"\nCreating playlists for user {USER_ID}…")
for pl in playlists:
    track_ids = []
    for track_name in pl["tracks"]:
        tid = name_to_id.get(track_name.lower())
        if not tid:
            # Partial match
            for name, tid2 in name_to_id.items():
                if name.startswith(track_name.lower()[:15]):
                    tid = tid2
                    break
        if tid:
            track_ids.append(tid)

    if not track_ids:
        print(f"  ⚠ No matched tracks for '{pl['name']}' — skipping")
        continue

    create_resp = requests.post(
        "https://api.spotify.com/v1/me/playlists",
        headers=h,
        json={
            "name": f"🗺 {pl['name']}",
            "description": "SoundMap mood playlist — auto-generated.",
            "public": False,
        },
    )
    if create_resp.status_code not in (200, 201):
        print(f"  ✗ Failed to create '{pl['name']}': {create_resp.status_code} {create_resp.text[:100]}")
        continue

    pl_id  = create_resp.json()["id"]
    pl_url = create_resp.json()["external_urls"]["spotify"]

    uris = [f"spotify:track:{tid}" for tid in track_ids]
    for i in range(0, len(uris), 100):
        requests.post(
            f"https://api.spotify.com/v1/playlists/{pl_id}/items",
            headers=h,
            json={"uris": uris[i:i+100]},
        )

    print(f"  ✓ '🗺 {pl['name']}' — {len(track_ids)} tracks → {pl_url}")

print("\nDone — check your Spotify library.")
