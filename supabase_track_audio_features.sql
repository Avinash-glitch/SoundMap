create table if not exists public.track_audio_features (
  spotify_id text primary key,
  reccobeats_id text,
  href text,
  isrc text,
  track_name text,
  artist text,
  album text,
  playlists jsonb not null default '[]'::jsonb,
  playlist_names text[] not null default '{}'::text[],
  acousticness double precision,
  danceability double precision,
  energy double precision,
  instrumentalness double precision,
  liveness double precision,
  loudness double precision,
  speechiness double precision,
  tempo double precision,
  valence double precision,
  musical_key integer,
  musical_mode integer,
  raw jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

create index if not exists track_audio_features_isrc_idx
  on public.track_audio_features (isrc);

create index if not exists track_audio_features_track_name_idx
  on public.track_audio_features (track_name);

create index if not exists track_audio_features_artist_idx
  on public.track_audio_features (artist);

create index if not exists track_audio_features_playlist_names_idx
  on public.track_audio_features using gin (playlist_names);

create index if not exists track_audio_features_energy_idx
  on public.track_audio_features (energy);

create index if not exists track_audio_features_valence_idx
  on public.track_audio_features (valence);

create index if not exists track_audio_features_tempo_idx
  on public.track_audio_features (tempo);
