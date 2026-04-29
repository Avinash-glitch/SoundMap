                 
  # SoundMap

  **Your music library, mapped.** SoundMap connects to your Spotify account and transforms your listening history into  
  an interactive 2D landscape — similar-sounding tracks cluster together, genres form territories, playlists form
  constellations.                                                                                                       
                  
  > No genre tags. No algorithmic recommendations. Just your music, visualised through audio science.                   
   
  ---                                                                                                                   
                  
  ## What it does

  - **Visualise your library** — up to 500 tracks plotted by audio DNA (tempo, energy, valence, danceability, and more) 
  using UMAP dimensionality reduction
  - **Explore interactively** — pan, zoom, and click through your musical landscape on a canvas-based map               
  - **AI mood zones** *(optional)* — Claude AI groups your library into mood-based territories                          
  - **Natural language curation** — describe a vibe, get a playlist: *"something for a late-night drive"*               
  - **Transfer playlists** *(coming soon)* — move any playlist from Spotify to Apple Music, or import a friend's Spotify
   playlist directly into your Apple Music library                                                                      
                                                                                                                        
  ---                                                                                                                   
                  
  ## Playlist Transfer

  One of SoundMap's upcoming features lets you bridge music platforms without losing your library:                      
   
  - **Spotify → Apple Music** — export any of your Spotify playlists to Apple Music in one click                        
  - **Friend's Spotify → your Apple Music** — paste a friend's public Spotify playlist URL and import it straight into
  your Apple Music library, no account sharing needed                                                                   
  - **Apple Music → Spotify** — coming in the same release
                                                                                                                        
  Tracks are matched using ISRC codes (universal track IDs) where available, with fuzzy title/artist fallback for       
  maximum accuracy.
                                                                                                                        
  ---             

  ## Tech stack

  | Layer | Tech |                                                                                                      
  |---|---|
  | Backend | Python · FastAPI |                                                                                        
  | Frontend | HTML · JavaScript (Canvas API) |
  | Audio analysis | Spotify Web API audio features |                                                                   
  | AI features | Anthropic Claude API *(optional)* |
  | Storage | Supabase or file-based |                                                                                  
  | Deployment | Railway |            
                                                                                                                        
  ---             
                                                                                                                        
  ## Getting started
                    
  ### Prerequisites
                   
  - Python 3.10+
  - A [Spotify Developer app](https://developer.spotify.com/dashboard) (Client ID + Secret)
  - *(Optional)* An [Anthropic API key](https://console.anthropic.com) for AI features     
                                                                                                                        
  ### Local setup                                                                                                       
                                                                                                                        
  ```bash                                                                                                               
  git clone https://github.com/Avinash-glitch/SoundMap.git                                                              
  cd SoundMap                                             
  pip install -r requirements.txt                                                                                       
  cp .env.example .env           
  # Fill in your Spotify credentials and optional Anthropic key
  uvicorn backend.main:app --reload                            
                                                                                                                        
  Then open http://localhost:8000.
                                                                                                                        
  Environment variables
                                                                                                                        
  SPOTIFY_CLIENT_ID=your_spotify_client_id
  SPOTIFY_CLIENT_SECRET=your_spotify_client_secret                                                                      
  SPOTIFY_REDIRECT_URI=http://localhost:8000/callback
  ANTHROPIC_API_KEY=optional_for_ai_features                                                                            
  SUPABASE_URL=optional_for_persistent_storage                                                                          
  SUPABASE_KEY=optional_for_persistent_storage
                                                                                                                        
  ---             
  Deploy to Railway
                   
  1. Fork this repo and connect it to https://railway.app
  2. Add the environment variables above in Railway's dashboard                                                         
  3. Set your Spotify app's redirect URI to your Railway deployment URL + /callback                                     
  4. Railway picks up Procfile and railway.toml automatically — no extra config needed                                  
                                                                                                                        
  ---                                                                                                                   
  Privacy                                                                                                               
                                                                                                                        
  - Spotify tokens are stored server-side only and never exposed to the client
  - User-provided AI API keys live in localStorage only — never sent to a database or logged                            
  - Maps are cached for 24 hours, then discarded                                                                        
  - No analytics, no data selling
                                                                                                                        
  ---             
  Roadmap                                                                                                               
                  
  - Spotify OAuth + library ingestion
  - UMAP audio feature mapping                                                                                          
  - Interactive canvas explorer
  - Claude AI mood zones                                                                                                
  - Natural language playlist curation
  - Playlist transfer: Spotify ↔ Apple Music                                                                            
  - Friend playlist import (public Spotify → your Apple Music)
  - Last.fm scrobble history overlay                                                                                    
  - Share your map as a public link                                                                                     
                                                                                                                        
  ---                                                                                                                   
  License                                                                                                               
                  
  MIT

  ---
