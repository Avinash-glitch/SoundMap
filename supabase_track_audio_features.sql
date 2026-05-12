create table if not exists public.track_audio_features (
  spotify_id text primary key,
  reccobeats_id text,
  href text,
  isrc text,
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

create index if not exists track_audio_features_energy_idx
  on public.track_audio_features (energy);

create index if not exists track_audio_features_valence_idx
  on public.track_audio_features (valence);

create index if not exists track_audio_features_tempo_idx
  on public.track_audio_features (tempo);
