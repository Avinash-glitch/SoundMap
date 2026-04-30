# SoundMap

**Your music library, mapped.** SoundMap connects to your Spotify account and transforms your playlists into an interactive 2D landscape — playlists with shared or similar artists cluster together, so your music taste arranges itself into a geography you can actually explore.

> No audio fingerprinting. No black-box recommendations. Just your playlists, mapped by what they have in common.

---

## What it does

- **Visualise your library** — up to 1000 tracks plotted based on artist genre tags and playlist membership, so playlists that share similar music end up close together on the map
- **Explore interactively** — pan, zoom, and click through your musical landscape on a canvas-based map
- **AI mood zones** *(free, no key needed)* — NVIDIA's free AI models group your library into mood-based territories automatically
- **Natural language curation** — describe a vibe, get a playlist: *"something for a late-night drive"*
- **Bring your own Claude key** *(optional)* — swap in Anthropic Claude for richer AI descriptions if you have an API key
- **Friend comparison** — connect with a friend's map using their SoundMap ID and see where your tastes overlap

---

## How the map works

SoundMap does **not** use Spotify's audio features (tempo, energy, valence etc.) — those require a deprecated API endpoint that is no longer available.

Instead, it builds the map from:

1. **Artist genre tags** — each track gets a genre vector from Spotify's artist metadata
2. **Playlist membership** — tracks in the same playlist are weighted 4× closer together in the UMAP graph, so playlists with shared songs cluster visually
3. **Popularity** — used as a lightweight tiebreaker dimension

The result: playlists with similar artists end up as neighbouring constellations. A hip-hop playlist and a rap playlist will sit side by side; your ambient and classical playlists will cluster away from your gym bangers.

---

## AI features

AI is powered by **NVIDIA's free NIM API** (GLM-5.1) by default — no key needed, it works for all users out of the box.

If you want to use Anthropic Claude instead (richer descriptions, better reasoning), you can add your own Claude API key in the settings panel. OpenAI is also supported.

| Provider | Key required | How to set |
|---|---|---|
| NVIDIA GLM-5.1 | No — free for all users | Set `NVIDIA_API_KEY` in `.env` (server-side) |
| Anthropic Claude | Yes — your own key | Enter in the UI settings panel |
| OpenAI | Yes — your own key | Enter in the UI settings panel |

---

## Tech stack

| Layer | Tech |
|---|---|
| Backend | Python · FastAPI |
| Frontend | HTML · JavaScript (Canvas API) |
| Map generation | Artist genre tags · Playlist membership · UMAP |
| AI features | NVIDIA NIM / GLM-5.1 (free) · Claude · OpenAI (optional) |
| Storage | Supabase or file-based |
| Deployment | Railway |

---

## Getting started

### Prerequisites

- Python 3.10+
- A [Spotify Developer app](https://developer.spotify.com/dashboard) (Client ID + Secret)
- *(Optional)* An NVIDIA NIM API key for server-side AI (users can also bring their own Claude/OpenAI key in the UI)

### Local setup

```bash
git clone https://github.com/Avinash-glitch/SoundMap.git
cd SoundMap
pip install -r requirements.txt
cp .env.example .env
# Fill in your Spotify credentials — see .env.example for all options
uvicorn backend.main:app --reload
```

Then open `http://localhost:8000`.

### Environment variables

```env
SPOTIFY_CLIENT_ID=your_spotify_client_id
SPOTIFY_CLIENT_SECRET=your_spotify_client_secret
SPOTIFY_REDIRECT_URI=http://localhost:8000/callback

# AI — NVIDIA is free and works for all users
NVIDIA_API_KEY=optional_server_side_ai

# Optional — users can also enter these in the UI
ANTHROPIC_API_KEY=optional
OPENAI_API_KEY=optional

# Optional — for persistent map storage
SUPABASE_URL=optional
SUPABASE_KEY=optional
```

---

## Deploy to Railway

1. Fork this repo and connect it to [Railway](https://railway.app)
2. Add the environment variables above in Railway's dashboard
3. Set your Spotify app's redirect URI to your Railway deployment URL + `/callback`
4. Railway picks up `Procfile` and `railway.toml` automatically — no extra config needed

---

## Privacy

- Spotify tokens are stored server-side only and never exposed to the client
- User-provided AI API keys live in `localStorage` only — never sent to a database or logged
- Maps are cached for 24 hours, then discarded
- No analytics, no data selling

---

## Roadmap

- [x] Spotify OAuth + library ingestion (up to 1000 tracks)
- [x] Genre + playlist-based UMAP mapping
- [x] Interactive canvas explorer
- [x] NVIDIA free AI mood zones
- [x] Natural language playlist curation
- [x] Friend map comparison via SoundMap ID
- [ ] Playlist transfer: Spotify ↔ Apple Music
- [ ] Friend playlist import (public Spotify → your Apple Music)
- [ ] Apple Music library mapping
- [ ] Share your map as a public link

---

## License

MIT
