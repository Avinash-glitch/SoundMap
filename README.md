# SoundMap

Your Spotify library as an interactive map. Similar-sounding tracks cluster together — genres form territories, playlists form constellations. Log in with Spotify and explore your taste as a landscape.

[**Try the demo →**](your-railway-url-here)

---

## What it does

- Connects to your Spotify account and pulls your library (up to 500 tracks)
- Computes audio embeddings from Spotify's audio features
- Runs UMAP to reduce them to 2D coordinates
- Renders an interactive canvas map you can pan, zoom, and explore
- Optionally groups your playlists into mood zones using Claude AI (your own API key)
- Lets you curate new playlists by describing what you want in plain English

---

## Self-hosting

### Prerequisites

- Python 3.10+
- A [Spotify Developer](https://developer.spotify.com/dashboard) app
- (Optional) An [Anthropic API key](https://console.anthropic.com) for AI features

### 1. Clone the repo

```bash
git clone https://github.com/Avinash-glitch/SoundMap.git
cd SoundMap
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Set up environment variables

Copy the example and fill in your values:

```bash
cp .env.example .env
```

```bash
# .env
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8000/auth/callback
SESSION_SECRET=any_random_string
APP_URL=http://127.0.0.1:8000
PORT=8000

# Optional — enables AI features server-side (users can also bring their own key)
ANTHROPIC_API_KEY=sk-ant-...

# Optional — persists maps across restarts
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-anon-key
```

### 4. Set up Spotify

1. Go to [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Create an app (or open an existing one)
3. Under **Edit Settings → Redirect URIs**, add: `http://127.0.0.1:8000/auth/callback`
4. Copy the **Client ID** and **Client Secret** into your `.env`

### 5. Run

```bash
uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

Visit `http://127.0.0.1:8000`, click **Connect Spotify**, and wait ~2 minutes for your map to generate.

---

## Deploying to Railway

### 1. Push to GitHub

```bash
git remote add origin https://github.com/your-username/SoundMap.git
git push -u origin main
```

### 2. Create a Railway project

1. Go to [railway.app](https://railway.app) and create a new project
2. Connect your GitHub repo
3. Railway will detect the `Procfile` and deploy automatically

### 3. Set environment variables in Railway

In your Railway service → **Variables**, add:

| Variable | Value |
|---|---|
| `SPOTIFY_CLIENT_ID` | from Spotify dashboard |
| `SPOTIFY_CLIENT_SECRET` | from Spotify dashboard |
| `SPOTIFY_REDIRECT_URI` | `https://your-app.railway.app/auth/callback` |
| `SESSION_SECRET` | any random string |
| `APP_URL` | `https://your-app.railway.app` |
| `SUPABASE_URL` | from Supabase dashboard → Settings → API |
| `SUPABASE_KEY` | anon key from Supabase dashboard |

### 4. Update Spotify redirect URI

In the Spotify Developer Dashboard, add your Railway URL as a redirect URI:
`https://your-app.railway.app/auth/callback`

### 5. Set up Supabase (optional but recommended)

Without Supabase, maps are stored on the Railway filesystem and lost on redeploy.

1. Create a project at [supabase.com](https://supabase.com)
2. Go to **SQL Editor** and run:

```sql
create table user_maps (
  user_id text primary key,
  map_data jsonb not null,
  updated_at timestamptz default now()
);

alter table user_maps disable row level security;
```

3. Get your **Project URL** and **anon key** from **Settings → API**
4. Add them to Railway as `SUPABASE_URL` and `SUPABASE_KEY`

---

## AI features

AI features use Claude (Anthropic) or GPT-4 (OpenAI). You can configure a server-side key via `ANTHROPIC_API_KEY`, or leave it unset and let users bring their own.

**Users are never required to have a key** — AI features are optional. The map works fully without them.

Keys entered by users are stored only in their browser's `localStorage`. They are never saved to your database or logged by the server.

### What the AI does

- **Mood Zones** — groups your playlists into 3–6 sonic territories (e.g. "Late Night Drive", "Peak Hour", "Sunday Morning"). Runs once when you enter your key on the loading screen.
- **AI Curate** — describe a playlist in plain English ("1 hour gym set, high BPM", "something for a late night drive") and Claude selects tracks from your library that fit.

---

## How it works

```
Spotify library
      ↓
Audio features (tempo, energy, valence, danceability…)
      ↓
UMAP → 2D coordinates
      ↓
Interactive canvas map
```

Each dot is a track. Tracks that sound similar are placed near each other. The clustering is entirely based on Spotify's audio analysis — no genre tags, no manual curation.

---

## Project structure

```
├── backend/
│   ├── main.py        # FastAPI app + all API routes
│   ├── auth.py        # Spotify OAuth 2.0 (PKCE)
│   ├── pipeline.py    # Audio features → UMAP → map data
│   ├── jobs.py        # Background job runner
│   ├── storage.py     # Supabase + file-based persistence
│   └── models.py      # Pydantic models
├── frontend/
│   ├── index.html     # Landing page
│   ├── loading.html   # Processing status + API key entry
│   └── map.html       # Interactive map
├── requirements.txt
├── Procfile
└── railway.toml
```

---

## Notes

- Maps are cached for 24 hours. Visiting again within that window skips reprocessing.
- Without Supabase, maps are stored in `./user_data/` and lost on Railway redeploy.
- The library is capped at 500 tracks for reasonable processing time (~2 min).
- Spotify tokens are stored server-side in the session and never exposed to the browser.
