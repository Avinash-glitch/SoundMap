alter table public.track_audio_features
  add column if not exists track_name text,
  add column if not exists artist text,
  add column if not exists album text,
  add column if not exists playlists jsonb not null default '[]'::jsonb,
  add column if not exists playlist_names text[] not null default '{}'::text[];

create index if not exists track_audio_features_track_name_idx
  on public.track_audio_features (track_name);

create index if not exists track_audio_features_artist_idx
  on public.track_audio_features (artist);

create index if not exists track_audio_features_playlist_names_idx
  on public.track_audio_features using gin (playlist_names);
