"""Storage falls back to files when Supabase is not configured."""
import json

import backend.storage as storage


def test_isrc_lookup_without_supabase_reads_the_file_cache(monkeypatch, tmp_path):
    cache = tmp_path / "audio_features.json"
    cache.write_text(json.dumps({
        "AAA": {"spotify_id": "AAA", "isrc": "QZ1234567890", "tempo": 120},
        "BBB": {"spotify_id": "BBB", "isrc": "QZ0000000000", "tempo": 90},
    }), encoding="utf-8")
    monkeypatch.setattr(storage, "_get_supabase", lambda: None)
    monkeypatch.setattr(storage, "AUDIO_FEATURE_CACHE", cache)

    rows = storage.load_track_audio_features_by_isrc(["qz1234567890"])
    assert set(rows) == {"QZ1234567890"}, "lookup should be case-insensitive"
    assert rows["QZ1234567890"]["spotify_id"] == "AAA"


def test_isrc_lookup_with_nothing_asked_for_is_empty(monkeypatch):
    monkeypatch.setattr(storage, "_get_supabase", lambda: None)
    assert storage.load_track_audio_features_by_isrc([]) == {}
    assert storage.load_track_audio_features_by_isrc(["", "  "]) == {}
