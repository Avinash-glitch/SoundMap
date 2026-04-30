# CLAUDE.md — SoundMap

## NEW FEATURE SPEC (implement this next)

### Overview
Four interconnected social/sharing features. Build them together — they share state.

---

### Feature 1 — User ID badge (copyable)

**Where:** `frontend/map.html` topbar, inside `.user-badge` div (around line 971).

**What:** Below the user's display name, show their SoundMap user ID (the `userId` JS variable, which is the Spotify user ID from the URL param `?user=`). Add a small copy-to-clipboard icon button next to it.

- Apple Music users have IDs ending in `_apple` — show their dot in red (`#fa3c44`) instead of green
- On copy: flash the icon green for 1.5s
- No backend changes needed — `userId` is already available as a JS variable

**HTML to add inside `.user-badge`:**
```html
<div style="display:flex;align-items:center;gap:.35rem;padding-left:1.1rem;margin-top:1px">
  <span id="uid-display" style="font-size:.6rem;color:var(--text-dim);cursor:pointer;user-select:all;letter-spacing:.01em" title="Your SoundMap ID — share this with friends"></span>
  <button id="btn-copy-uid" title="Copy ID" style="background:none;border:none;padding:2px;cursor:pointer;color:var(--text-dim);display:flex;align-items:center;opacity:.5;transition:opacity .15s,color .15s">
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
  </button>
</div>
```

**JS to add in `loadMap()` after setting `user-name`:**
```js
const uidEl = document.getElementById('uid-display');
if (uidEl) uidEl.textContent = userId;
```

**JS event listener (add near other button listeners):**
```js
document.getElementById('btn-copy-uid')?.addEventListener('click', () => {
  navigator.clipboard.writeText(userId).then(() => {
    const btn = document.getElementById('btn-copy-uid');
    btn.style.color = 'var(--accent)';
    btn.style.opacity = '1';
    setTimeout(() => { btn.style.color = ''; btn.style.opacity = ''; }, 1500);
  });
});
```

---

### Feature 2 — Split brain compare view

**Context:** The app already has a "Compare" button (`#btn-compare`) that opens a modal (`#compare-modal`) where users enter a friend's user ID. This loads the friend's map data into `comparePoints` (JS array), `compareDisplayName` (string), `friendMapPlaylists` (array), and `_friendMapData` (full map object — add this). It also fetches comparison data from `GET /compare/{myId}/{friendId}` and stores it in `_cmpData` (add this).

Currently `render()` is:
```js
function render() {
  if (comparePoints.length) { _hideBrainOverlay(); renderWithCompare(); return; }
  if (brainMode) { _showBrainOverlay(); return; }
  _hideBrainOverlay();
  _renderDots();
}
```

**Change `render()` to:**
```js
function render() {
  if (comparePoints.length && brainMode) { _hideBrainOverlay(); _showCompareBrainView(); return; }
  if (comparePoints.length) { _hideBrainOverlay(); _hideCompareBrainView(); renderWithCompare(); return; }
  _hideCompareBrainView();
  if (brainMode) { _showBrainOverlay(); return; }
  _hideBrainOverlay();
  _renderDots();
}
```

**New state variables to add (near existing `let _svgBrainCache = null`):**
```js
let _svgFriendBrainCache = null;
let _svgFriendBrainSelected = null;
let _friendColorsMap = {};
let _cmpData = null;
let _friendMapData = null;
```

**Update `loadCompare(friendId)` function** (already exists ~line 3709) to also save:
```js
_friendMapData = data;          // save full map data (before comparePoints = ...)
_svgFriendBrainCache = null;   // reset cache on new friend load
_friendColorsMap = {};
_cmpData = null;
```
And after fetching comparison data:
```js
_cmpData = cData;
```

**Update compare-badge click handler** (clears compare state) to also add:
```js
_friendMapData = null;
_svgFriendBrainCache = null;
_friendColorsMap = {};
_cmpData = null;
_hideCompareBrainView();
```

**New HTML — add after `<div id="brain-info-panel"></div>`:**
```html
<div id="cmp-brain-view">
  <div class="cmp-names-row">
    <div class="cmp-name-col" id="cmp-name-me"><div class="dot"></div><span></span></div>
    <div class="cmp-name-sep">vs</div>
    <div class="cmp-name-col" id="cmp-name-friend"><div class="dot" style="background:#a78bfa"></div><span></span></div>
  </div>
  <div class="cmp-main">
    <div class="cmp-half" id="cmp-half-me">
      <svg id="cmp-svg-me" viewBox="0 0 1000 680" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg"></svg>
      <div class="cmp-half-legend" id="cmp-legend-me"></div>
    </div>
    <div class="cmp-center" id="cmp-center"></div>
    <div class="cmp-half" id="cmp-half-friend">
      <svg id="cmp-svg-friend" viewBox="0 0 1000 680" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg"></svg>
      <div class="cmp-half-legend" id="cmp-legend-friend"></div>
    </div>
  </div>
</div>
```

**New CSS (add after `.bpi-conn-item:hover { }`):**
```css
#cmp-brain-view {
  display: none;
  position: fixed;
  top: 52px; left: 0; right: 0; bottom: 0;
  z-index: 6;
  background: var(--bg);
  flex-direction: column;
}
.cmp-names-row {
  height: 32px; display: flex; align-items: center;
  border-bottom: 1px solid var(--border); flex-shrink: 0;
}
.cmp-name-col {
  flex: 1; display: flex; align-items: center; justify-content: center;
  gap: 6px; font-size: 10px; font-family: 'Syne', sans-serif; font-weight: 600;
  letter-spacing: .05em; color: var(--text-dim); text-transform: uppercase;
  overflow: hidden; white-space: nowrap; text-overflow: ellipsis; padding: 0 12px;
}
.cmp-name-col .dot { width:7px;height:7px;border-radius:50%;background:var(--accent);flex-shrink:0; }
.cmp-name-sep {
  width: 230px; flex-shrink: 0; display: flex; align-items: center; justify-content: center;
  font-size: 8px; letter-spacing: .1em; color: rgba(255,255,255,.2); text-transform: uppercase;
  border-left: 1px solid var(--border); border-right: 1px solid var(--border);
}
.cmp-main { flex: 1; display: flex; overflow: hidden; min-height: 0; }
.cmp-half { flex: 1; position: relative; overflow: hidden; min-width: 0; }
.cmp-half svg {
  position: absolute; left: 0; right: 0; top: 0; bottom: 44px;
  width: 100%; height: calc(100% - 44px);
}
.cmp-half-legend {
  position: absolute; bottom: 0; left: 0; right: 0; height: 44px;
  display: flex; align-items: center; padding: 0 10px; gap: 8px;
  overflow-x: auto; scrollbar-width: none; border-top: 1px solid var(--border);
}
.cmp-half-legend::-webkit-scrollbar { display: none; }
.cmp-center {
  width: 230px; flex-shrink: 0; display: flex; flex-direction: column;
  align-items: center; padding: 20px 14px; gap: 12px;
  border-left: 1px solid var(--border); border-right: 1px solid var(--border);
  background: var(--bg2); overflow-y: auto; scrollbar-width: thin;
}
.cmp-ring-wrap { position: relative; width: 96px; height: 96px; flex-shrink: 0; }
.cmp-ring-wrap svg { transform: rotate(-90deg); }
.cmp-ring-label {
  position: absolute; inset: 0; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 1px;
}
.cmp-ring-pct { font-family: 'Syne', sans-serif; font-weight: 700; font-size: 1.3rem; line-height: 1; }
.cmp-ring-sub { font-size: 7px; letter-spacing: .1em; text-transform: uppercase; color: var(--text-dim); }
.cmp-pl-pair {
  display: grid; grid-template-columns: 1fr auto 1fr; gap: 4px;
  align-items: center; padding: 4px 0;
  border-bottom: 1px solid rgba(255,255,255,.05); cursor: pointer; width: 100%;
}
.cmp-pl-pair:last-child { border-bottom: none; }
.cmp-pl-name { font-size: 9px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-dim); }
.cmp-pl-name.r { text-align: right; }
.cmp-pl-score {
  font-size: 8px; color: var(--accent2); background: rgba(124,106,247,.15);
  border-radius: 3px; padding: 1px 4px; white-space: nowrap; text-align: center;
}
.cmp-section-title {
  font-size: 8px; text-transform: uppercase; letter-spacing: .1em;
  color: rgba(255,255,255,.3); width: 100%; text-align: center; flex-shrink: 0;
}
```

---

### Feature 3 — Center comparison panel (compatibility ring + playlist pairs)

Built inside `_buildCmpCenter(d)` which renders into `#cmp-center`.

`d` is the response from `GET /compare/{userA}/{userB}` — already fetched by `loadCompare`. Shape:
```js
{
  compatibility: 0.72,        // 0–1 float
  shared_count: 18,           // int
  shared_tracks: [...],       // array of track objects
  similar_playlists: [        // array of {playlist_a, playlist_b, score}
    { playlist_a: "Chill", playlist_b: "Evening Vibes", score: 0.84 },
    ...
  ],
  user_a: { id, display_name, track_count },
  user_b: { id, display_name, track_count },
}
```

**`_buildCmpCenter(d)` implementation:**
```js
function _buildCmpCenter(d) {
  const el = document.getElementById('cmp-center');
  if (!el || !d) return;
  const pct = Math.round(d.compatibility * 100);
  const C = 2 * Math.PI * 38;
  const offset = C * (1 - d.compatibility);
  const color = pct >= 70 ? '#34d399' : pct >= 40 ? '#a78bfa' : '#f87171';

  const pairsHtml = (d.similar_playlists || []).slice(0, 8).map(p => `
    <div class="cmp-pl-pair">
      <div class="cmp-pl-name" title="${_esc(p.playlist_a)}">${_esc(p.playlist_a)}</div>
      <div class="cmp-pl-score">${Math.round(p.score * 100)}%</div>
      <div class="cmp-pl-name r" title="${_esc(p.playlist_b)}">${_esc(p.playlist_b)}</div>
    </div>`).join('');

  el.innerHTML = `
    <div class="cmp-ring-wrap">
      <svg width="96" height="96" viewBox="0 0 96 96">
        <circle cx="48" cy="48" r="38" fill="none" stroke="var(--border)" stroke-width="9"/>
        <circle cx="48" cy="48" r="38" fill="none" stroke="${color}" stroke-width="9"
          stroke-linecap="round" stroke-dasharray="${C.toFixed(1)}" stroke-dashoffset="${offset.toFixed(1)}"/>
      </svg>
      <div class="cmp-ring-label">
        <span class="cmp-ring-pct" style="color:${color}">${pct}%</span>
        <span class="cmp-ring-sub">match</span>
      </div>
    </div>
    <div style="font-size:10px;color:var(--accent);text-align:center">${d.shared_count} shared tracks</div>
    ${pairsHtml ? `<div class="cmp-section-title">Similar Playlists</div><div style="width:100%">${pairsHtml}</div>` : ''}
    <div style="font-size:8px;color:rgba(255,255,255,.2);text-align:center;line-height:1.4;margin-top:4px">Click a lobe on the right<br>to copy your friend's playlist</div>
  `;
}
```

---

### Feature 4 — Copy friend's playlist to Spotify

When user clicks a lobe on the **right (friend's) brain**, show `#brain-info-panel` with the lobe's playlist info plus an "Add to my Spotify" button.

The `/import-friend-playlist` backend endpoint already exists:
```
POST /import-friend-playlist
Body: { playlist_name, track_ids, friend_display_name }
Returns: { name, track_count, url }
```

**`_showFriendLobePanel(idx, name, cache, pColors)` implementation:**
```js
function _showFriendLobePanel(idx, name, cache, pColors) {
  const panel = document.getElementById('brain-info-panel');
  const { N } = cache;
  const col = pColors[name] || COLOR_UNTAGGED;
  const trackIds = comparePoints
    .filter(p => (p.playlists || []).includes(name))
    .map(p => p.id).filter(Boolean);
  const trackCount = trackIds.length;
  const side = idx < Math.ceil(N / 2) ? 'LEFT' : 'RIGHT';

  panel.innerHTML = `<div class="bpi-inner">
    <div>
      <div style="font-size:9px;color:var(--text-dim);letter-spacing:.14em;text-transform:uppercase;margin-bottom:5px">
        ${_esc(compareDisplayName)}'s ${side} LOBE
      </div>
      <div style="font-family:Syne,sans-serif;font-weight:700;font-size:20px;line-height:1.2;color:${col};text-shadow:0 0 28px ${col}40">
        ${_esc(name)}
      </div>
    </div>
    <div style="width:44px;height:3px;border-radius:2px;background:${col};opacity:.5"></div>
    <div style="padding:12px 8px;background:var(--surface);border-radius:8px;text-align:center">
      <div style="font-family:Syne,sans-serif;font-weight:700;font-size:24px;color:${col}">${trackCount}</div>
      <div style="font-size:9px;color:var(--text-dim);margin-top:3px;letter-spacing:.1em">TRACKS</div>
    </div>
    <button id="bpi-copy-spotify" style="padding:10px;background:var(--accent);border:none;border-radius:8px;color:#000;font-family:'DM Mono',monospace;font-size:11px;font-weight:600;cursor:pointer;width:100%;transition:opacity .15s">
      Add to my Spotify
    </button>
    <div id="bpi-copy-status" style="font-size:10px;color:var(--text-dim);text-align:center;display:none;line-height:1.5"></div>
    <button id="bpi-copy-desel" style="padding:8px;background:transparent;border:none;color:var(--text-dim);font-family:'DM Mono',monospace;font-size:11px;cursor:pointer;width:100%;text-align:center">
      Close
    </button>
  </div>`;

  panel.classList.add('open');

  panel.querySelector('#bpi-copy-spotify').addEventListener('click', async () => {
    const btn = panel.querySelector('#bpi-copy-spotify');
    const status = panel.querySelector('#bpi-copy-status');
    btn.disabled = true;
    btn.textContent = 'Copying…';
    status.style.display = 'block';
    status.textContent = `Adding ${trackCount} tracks…`;
    try {
      const resp = await fetch('/import-friend-playlist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ playlist_name: name, track_ids: trackIds, friend_display_name: compareDisplayName }),
      });
      if (!resp.ok) throw new Error();
      const d = await resp.json();
      status.style.color = 'var(--accent)';
      status.textContent = `Added ${d.track_count} tracks to "${d.name}"`;
      btn.textContent = 'Done!';
    } catch {
      status.style.color = '#f87171';
      status.textContent = 'Failed — are you logged in to Spotify?';
      btn.disabled = false;
      btn.textContent = 'Retry';
    }
  });

  panel.querySelector('#bpi-copy-desel').addEventListener('click', () => {
    panel.classList.remove('open');
    _svgFriendBrainSelected = null;
  });
}
```

---

### The brain rendering helpers needed

The existing `_computeSVGBrainData()` and `_buildBrainSVGMarkup()` use globals (`points`, `mapData`, `playlistColors`). You need generalized versions for the compare view.

**Add `_buildPlaylistColorsFor(playlists)`:**
```js
function _buildPlaylistColorsFor(playlists) {
  const map = {};
  (playlists || []).forEach((pl, i) => {
    const hue = Math.round((i / Math.max(playlists.length, 1)) * 360);
    map[pl.name || pl] = `hsl(${hue}, 70%, 62%)`;
  });
  return map;
}
```

**Add `_computeSVGBrainDataFor(pts, md)` — same logic as `_computeSVGBrainData()` but takes explicit params instead of globals.**

Then update existing `_computeSVGBrainData()` to just call:
```js
function _computeSVGBrainData() {
  return _computeSVGBrainDataFor(points, mapData);
}
```

**Add `_buildBrainSVGMarkupFor(cache, pColors, pfx)` — same logic as `_buildBrainSVGMarkup()` but:**
- Takes `cache`, `pColors`, `pfx` as parameters instead of reading globals
- Prefixes ALL SVG IDs with `pfx` (e.g. `pfx + 'bcp'`, `pfx + 'rg' + i`, etc.)
- This prevents ID conflicts when both SVGs are in the DOM simultaneously
- Use prefix `'cm'` for my brain in compare view, `'cf'` for friend's brain
- Does NOT include the hint text (that's only for the solo brain view)

**Add `_wireBrainSVGEventsOn(svgEl, cache, pColors, pfx, isFriend)` — same logic as `_wireBrainSVGEvents()` but:**
- Works on any SVG element, not just `#brain-svg`
- Uses `pfx` to find the right IDs (`#${pfx}rg${idx}a` etc.)
- If `isFriend === true`: click calls `_showFriendLobePanel(idx, name, cache, pColors)` instead of `_showBrainInfoPanel`; no dblclick drilldown
- If `isFriend === false`: click calls `_showBrainInfoPanel(idx, name)` (existing); dblclick calls `_openDrillDown(name)`
- Track count for hover label: `isFriend ? comparePoints : points`

**Add `_buildCmpLegend(containerId, plNames, pColors)`:**
```js
function _buildCmpLegend(containerId, plNames, pColors) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = plNames.map(name => {
    const col = pColors[name] || COLOR_UNTAGGED;
    return `<div style="display:flex;align-items:center;gap:5px;flex-shrink:0;font-size:9px;color:var(--text-dim);white-space:nowrap">
      <div style="width:6px;height:6px;border-radius:50%;background:${col};box-shadow:0 0 6px ${col}80;flex-shrink:0"></div>
      ${_esc(name)}</div>`;
  }).join('');
}
```

**Add `_showCompareBrainView()` and `_hideCompareBrainView()`:**
```js
function _hideCompareBrainView() {
  document.getElementById('cmp-brain-view').style.display = 'none';
}

function _showCompareBrainView() {
  const view = document.getElementById('cmp-brain-view');
  view.style.display = 'flex';
  canvas.style.opacity = '0';

  // Set names in header
  document.querySelector('#cmp-name-me span').textContent = mapData?.display_name || userId;
  document.querySelector('#cmp-name-friend span').textContent = compareDisplayName;

  // My brain (left)
  const svgMe = document.getElementById('cmp-svg-me');
  const myKey = 'cm' + (points.length || 0);
  if (svgMe.dataset.built !== myKey) {
    if (!_svgBrainCache) _svgBrainCache = _computeSVGBrainData();
    if (_svgBrainCache) {
      svgMe.innerHTML = _buildBrainSVGMarkupFor(_svgBrainCache, playlistColors, 'cm');
      svgMe.dataset.built = myKey;
      _wireBrainSVGEventsOn(svgMe, _svgBrainCache, playlistColors, 'cm', false);
      _buildCmpLegend('cmp-legend-me', _svgBrainCache.plNames, playlistColors);
    }
  }

  // Friend's brain (right)
  const svgFriend = document.getElementById('cmp-svg-friend');
  const friendKey = 'cf' + (comparePoints.length || 0);
  if (svgFriend.dataset.built !== friendKey && _friendMapData) {
    if (!_svgFriendBrainCache) {
      _svgFriendBrainCache = _computeSVGBrainDataFor(comparePoints, _friendMapData);
      _friendColorsMap = _buildPlaylistColorsFor(_friendMapData.playlists || []);
    }
    if (_svgFriendBrainCache) {
      svgFriend.innerHTML = _buildBrainSVGMarkupFor(_svgFriendBrainCache, _friendColorsMap, 'cf');
      svgFriend.dataset.built = friendKey;
      _wireBrainSVGEventsOn(svgFriend, _svgFriendBrainCache, _friendColorsMap, 'cf', true);
      _buildCmpLegend('cmp-legend-friend', _svgFriendBrainCache.plNames, _friendColorsMap);
    }
  }

  // Center panel
  if (_cmpData) _buildCmpCenter(_cmpData);
}
```

---

### Notes for the implementing AI

- The main brain SVG (`#brain-svg`) uses bare IDs like `bcp`, `rg0`, `gl0gb`. The compare view uses `cm` and `cf` prefixes. All three can be in the DOM at once without conflict.
- `comparePoints` holds the **normalised** friend's points (same shape as `points` with `nx`, `ny`). The friend's `mapData` equivalent is `_friendMapData`.
- The `_hideBrainOverlay()` function already hides `#brain-overlay` and restores canvas opacity. `_hideCompareBrainView()` just hides `#cmp-brain-view`. Both should be called in the right places so they don't fight each other.
- The `compare-badge` click handler (line ~3928) clears compare state — make sure it also clears all the new state vars and calls `_hideCompareBrainView()`.
- In `loadCompare()` (~line 3709), `_friendMapData = data` must be set BEFORE `render()` is called, or `_showCompareBrainView()` won't have the data it needs.
- `_cmpData` should be set after the `/compare/{a}/{b}` fetch resolves, then call `_buildCmpCenter(_cmpData)` if the compare brain view is currently visible.
- When the user toggles back to scatter mode (btn-brain click) while compare is active, `_hideCompareBrainView()` should be called.

---

## Previously completed (do not redo)

- Brain map is the default view. `brainMode = true` on init.
- The "Brain" button in the topbar now says "Scatter" and toggles back to the dot scatter view.
- Clicking a playlist in the left sidebar highlights that lobe on the brain map (`_selectBrainLobeByName`, `_clearBrainLobeHighlight`).
- Mood zones tab and playlist tab removed — brain map IS the primary view.

---

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
