# CLAUDE.md — SoundMap "Electrocuting the Night"

## What This Is

SoundMap is a hosted web app that visualises a user's Spotify library as a 2D
interactive map. Similar-sounding tracks cluster together. Users log in with
Spotify, their library gets processed, and they see their personal music taste
as an explorable landscape.

The core insight: every track has a sonic "position" computed from audio
embeddings (CLAP) or Spotify audio features. UMAP reduces these to 2D
coordinates. The result is a map where genres form territories, playlists
form constellations, and users can explore music they've never heard by
navigating the space around their existing taste.

---

## Current State

The following already exists and works. Do not rewrite these unless explicitly
asked:

- `pipeline.py` — fetches Spotify library, computes embeddings, runs UMAP,
  outputs `map_data.json`
- `soundmap.html` — frontend visualiser, reads `map_data.json`, full canvas
  rendering, sidebar, search, legend, zoom/pan
- `soundmap-demo.html` — standalone demo with 82 hardcoded tracks, no data
  needed

---

## What Needs Building

Transform this from a local script into a hosted multi-user web app where
anyone visits the URL, clicks "Connect Spotify", and gets their personal map.

---

## Architecture

```
soundmap/
├── backend/
│   ├── main.py              # FastAPI app — entry point
│   ├── auth.py              # Spotify OAuth 2.0 flow
│   ├── pipeline.py          # Existing pipeline, adapted for per-user use
│   ├── jobs.py              # Background job processing with RQ
│   ├── storage.py           # User map data persistence (Supabase)
│   ├── models.py            # Pydantic models
│   └── requirements.txt
├── frontend/
│   ├── index.html           # Landing page + login
│   ├── loading.html         # Processing status page
│   └── map.html             # The visualiser (adapted from soundmap.html)
├── .env.example
├── Procfile                 # For Railway/Render deployment
├── railway.toml             # Railway config
└── CLAUDE.md                # This file
```

---

## Backend — FastAPI

### `main.py`

FastAPI app with these routes:

```
GET  /                      → serves frontend/index.html
GET  /auth/login            → redirects to Spotify OAuth
GET  /auth/callback         → handles OAuth callback, starts processing job
GET  /status/{job_id}       → returns job status (queued/processing/done/error)
GET  /map/{user_id}         → returns map_data.json for a user
GET  /map.html              → serves the map visualiser
GET  /loading.html          → serves the loading page
```

CORS enabled for all origins in development. Lock down in production.

### `auth.py`

Spotify OAuth 2.0 with PKCE — no client secret exposed to frontend.

Scopes needed:
```
user-library-read
playlist-read-private
user-top-read
user-read-recently-played
user-read-private
```

Flow:
1. `/auth/login` generates code_verifier + code_challenge, stores in session,
   redirects to Spotify
2. `/auth/callback` receives code, exchanges for token using PKCE verifier,
   gets user profile, triggers background job, redirects to `/loading.html?job={id}`

Store tokens in session (use `starlette.middleware.sessions` with a secret key).
Never expose tokens to the frontend.

### `jobs.py`

Use Python `concurrent.futures.ThreadPoolExecutor` for background processing.
Keep it simple — no Redis dependency for V1, just in-memory job tracking with
a dict. Good enough for low traffic.

```python
jobs = {}  # job_id → {status, progress, user_id, error}
```

Job statuses: `queued` → `processing` → `done` | `error`

Job runs `pipeline.process_user(token, user_id)` and updates status dict.

### `pipeline.py` changes

Adapt existing pipeline.py for programmatic use:

```python
def process_user(spotify_token: str, user_id: str) -> dict:
    """
    Runs full pipeline for a user.
    Returns map_data dict (not a file).
    Uses CLAP if available, falls back to audio features.
    Saves result to storage.
    """
```

Add progress callbacks so the frontend can show real progress:
```python
def process_user(spotify_token, user_id, on_progress=None):
    # on_progress(pct, message) called throughout
```

Cap library at 500 tracks for V1 to keep processing time reasonable.
Take: all playlists up to 500 tracks total, prioritising most recently played.

### `storage.py`

Simple file-based storage for V1 — no database needed yet.

```python
STORAGE_DIR = Path("./user_data")

def save_map(user_id: str, map_data: dict) -> None
def load_map(user_id: str) -> dict | None
def map_exists(user_id: str) -> bool
def map_age_hours(user_id: str) -> float
```

Store as `./user_data/{user_id}.json`.
If map is less than 24 hours old, serve cached version — don't reprocess.
Add `.gitignore` entry for `user_data/`.

---

## Frontend

### `index.html` — Landing Page

Design direction: dark, cinematic, confident. Not a SaaS landing page.
More like a music visualiser tool with a strong point of view.

Must have:
- The name "SoundMap" prominently
- Tagline: "Your music as a landscape"
- Single CTA button: "Connect Spotify"
- Brief explanation of what it does (2-3 sentences max)
- Link to demo: "See a demo first →" opens soundmap-demo.html
- Shows example map screenshot or the animated demo canvas in background
- Mobile responsive

Font: Syne (headings) + DM Mono (body) — already used in the visualiser.
Colour: same CSS variables as soundmap.html for visual consistency.

No marketing waffle. No feature lists. No pricing. Just the product.

### `loading.html` — Processing Status

Shown while the user's library is being processed.

Polls `GET /status/{job_id}` every 2 seconds.

Shows:
- Animated visualisation (use the demo canvas in background, blurred)
- Current status message from the job
- Progress bar
- Estimated time remaining (rough: ~2 min for audio features, ~20 min for CLAP)

On job completion: auto-redirects to `/map.html?user={user_id}`

On error: shows error message with a "Try Again" link back to `/`

### `map.html` — The Visualiser

Adapted from existing `soundmap.html`. Key changes:

1. On load, reads `user_id` from URL params
2. Fetches map data from `GET /map/{user_id}` instead of `map_data.json`
3. Adds a top-right "Your Library" badge showing the user's Spotify display name
4. Adds a "Refresh" button that triggers reprocessing (calls `/auth/login` again)
5. Everything else stays exactly the same as the existing visualiser

---

## Environment Variables

```bash
# .env.example
SPOTIFY_CLIENT_ID=your_client_id_here
SPOTIFY_CLIENT_SECRET=your_client_secret_here
SPOTIFY_REDIRECT_URI=http://localhost:8000/auth/callback
SESSION_SECRET=generate_a_random_string_here
APP_URL=http://localhost:8000
PORT=8000
```

For production deployment on Railway:
```
SPOTIFY_REDIRECT_URI=https://your-app.railway.app/auth/callback
APP_URL=https://your-app.railway.app
```

---

## Dependencies

```
# requirements.txt
fastapi>=0.109.0
uvicorn[standard]>=0.27.0
spotipy>=2.23.0
umap-learn>=0.5.6
numpy>=1.26.0
scipy>=1.12.0
scikit-learn>=1.4.0
requests>=2.31.0
python-dotenv>=1.0.0
starlette>=0.36.0
python-multipart>=0.0.9
httpx>=0.26.0
```

CLAP is optional — the app works without it. Do not add msclap to
requirements.txt. Instead detect it at runtime:

```python
try:
    from msclap import CLAP
    CLAP_AVAILABLE = True
except ImportError:
    CLAP_AVAILABLE = False
```

---

## Deployment — Railway

`Procfile`:
```
web: uvicorn backend.main:app --host 0.0.0.0 --port $PORT
```

`railway.toml`:
```toml
[build]
builder = "nixpacks"

[deploy]
startCommand = "uvicorn backend.main:app --host 0.0.0.0 --port $PORT"
restartPolicyType = "on-failure"
```

The `user_data/` directory will not persist across Railway deploys — that is
acceptable for V1. Add a note in README that maps regenerate on redeploy.
V2 will add Supabase for persistence.

---

## Build Order

Build in this exact sequence. Each stage should be fully working before
moving to the next. Do not skip ahead.

**Stage 1 — Backend skeleton**
FastAPI app boots, serves index.html, `/auth/login` redirects to Spotify,
`/auth/callback` receives the code and logs the user's display name.
Test: visit localhost:8000, click login, see your name logged in terminal.

**Stage 2 — Pipeline integration**
`/auth/callback` triggers background job, job runs pipeline for the user,
saves to `user_data/{user_id}.json`. Status endpoint returns progress.
Test: complete auth flow, watch pipeline run, see JSON file appear.

**Stage 3 — Loading page**
`loading.html` polls status endpoint, shows progress, auto-redirects on
completion.
Test: full flow from login → loading → map loads with real data.

**Stage 4 — Map page**
`map.html` fetches user's map data from API, renders correctly.
Test: map shows with your real Spotify library data.

**Stage 5 — Frontend polish**
`index.html` looks good, loading page looks good, mobile responsive.
Test: show it to someone who has never seen it — they should understand
what it does and be able to use it without explanation.

**Stage 6 — Deploy**
Push to Railway. Set environment variables. Test full flow on production URL.
Update `SPOTIFY_REDIRECT_URI` in both `.env` and Spotify Developer Dashboard.

---

## Important Constraints

**Never store raw audio.** Preview files are streamed into memory for
embedding generation and immediately discarded. Never write audio to disk.

**Never expose Spotify tokens to the frontend.** All API calls go through
the backend. The frontend only ever sees `user_id` and `job_id`.

**Rate limiting.** Spotify audio features endpoint: batch in groups of 100,
add 100ms delay between batches. Preview URL fetching: max 5 concurrent.

**Error handling.** Every pipeline step should catch exceptions and update
job status to `error` with a human-readable message. Users should never see
a Python traceback.

**The visualiser must work offline** once map data is loaded. Do not make
API calls from inside the canvas render loop.

**Mobile.** The map is best on desktop but must not break on mobile.
Touch pan and pinch zoom already work in the existing code.

---

## What NOT to Build

Do not build any of the following unless explicitly asked:

- User accounts / passwords / email auth — Spotify OAuth is sufficient
- Social features / sharing / following
- Playlist creation or modification
- Any feature that writes back to the user's Spotify
- Payment / subscription / rate limiting
- Analytics / tracking
- ElevenLabs integration (planned for V2)
- CLAP auto-installation
- Android / React Native app
- Any AI chat interface

---

## V2 Features (Do Not Build Now, But Design For)

The architecture should not prevent these from being added later:

- ElevenLabs narration: when a user selects a track, a voice describes
  the sonic neighbourhood they're in. Needs no architectural changes —
  just a new API call from the sidebar.
- Playlist matcher: drop a new track, see which playlists it fits.
  Pipeline already computes centroids — just needs an endpoint.
- Supabase storage: swap `storage.py` file operations for Supabase client.
  Keep the same interface so nothing else changes.
- CLAP embeddings: already handled with runtime detection. Just needs
  the server to have msclap installed.

---

## Code Style

- Python: type hints everywhere, docstrings on public functions
- No commented-out code
- Environment variables via `python-dotenv`, never hardcoded
- Frontend: vanilla JS only, no frameworks, same style as existing files
- CSS: same variables as existing (`--bg`, `--accent`, etc.)
- Consistent with existing code aesthetic — clean, minimal, purposeful

---

## If Something Is Unclear

Default to the simpler implementation. This is V1. It does not need to
scale to millions of users. It needs to work reliably for one person and
be ready to show to a small group of early users.

When in doubt: make it work first, make it elegant second.
