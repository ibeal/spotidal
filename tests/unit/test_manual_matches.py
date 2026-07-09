import yaml
import pytest

from spotidal.cache import MatchFailureDatabase, TrackMatchCache
from spotidal.manual_matches import add_not_found_match, import_manual_matches
from spotidal.type.models import Album, Artist, Track


def _track(track_id: str) -> Track:
    artist = Artist("Artist")
    return Track(track_id, "Song", [artist], Album("Album", [artist]), None, 180.0, 1, None, True)


def test_add_not_found_match_writes_editable_queue_and_deduplicates(tmp_path):
    queue_path = tmp_path / "songs_not_found.yml"

    add_not_found_match(queue_path, "Playlist", "Tidal", "Spotify", _track("td-1"))
    add_not_found_match(queue_path, "Playlist", "Tidal", "Spotify", _track("td-1"))

    assert yaml.safe_load(queue_path.read_text()) == {
        "matches": [{
            "playlist": "Playlist", "artist": "Artist", "title": "Song",
            "tidal_id": "td-1", "spotify_id": None,
        }],
    }


def test_import_manual_matches_persists_both_directions_removes_completed_rows_and_failures(tmp_path):
    queue_path = tmp_path / "songs_not_found.yml"
    cache_path = tmp_path / ".cache.db"
    queue_path.write_text(yaml.safe_dump({"matches": [
        {"playlist": "Playlist", "artist": "Artist", "title": "Completed", "tidal_id": "td-1", "spotify_id": "sp-1"},
        {"playlist": "Playlist", "artist": "Artist", "title": "Pending", "tidal_id": "td-2", "spotify_id": None},
    ]}, sort_keys=False))
    failures = MatchFailureDatabase(str(cache_path))
    failures.cache_match_failure("td-1")

    imported, remaining, migrated = import_manual_matches(queue_path, cache_path)

    assert (imported, remaining, migrated) == (1, 1, False)
    assert TrackMatchCache(str(cache_path), "tidal", "spotify").get("td-1") == "sp-1"
    assert TrackMatchCache(str(cache_path), "spotify", "tidal").get("sp-1") == "td-1"
    assert failures.has_match_failure("td-1") is False
    assert yaml.safe_load(queue_path.read_text())["matches"][0]["tidal_id"] == "td-2"


def test_import_manual_matches_migrates_legacy_text_log(tmp_path):
    queue_path = tmp_path / "songs_not_found.yml"
    (tmp_path / "songs_not_found.txt").write_text(
        "=== Songs not found on playlist Playlist on Spotify ===\n"
        "td-1: Artist - Song\n"
    )

    imported, remaining, migrated = import_manual_matches(queue_path, tmp_path / ".cache.db")

    assert (imported, remaining, migrated) == (0, 1, True)
    assert yaml.safe_load(queue_path.read_text())["matches"] == [{
        "playlist": "Playlist", "artist": "Artist", "title": "Song",
        "tidal_id": "td-1", "spotify_id": None,
    }]


def test_import_manual_matches_uses_legacy_log_from_the_original_working_directory(tmp_path):
    queue_path = tmp_path / "data" / "songs_not_found.yml"
    queue_path.parent.mkdir()
    legacy_path = tmp_path / "songs_not_found.txt"
    legacy_path.write_text(
        "=== Songs not found on playlist Playlist on Tidal ===\n"
        "sp-1: Artist - Song\n"
    )

    imported, remaining, migrated = import_manual_matches(
        queue_path, tmp_path / ".cache.db", legacy_queue_path=legacy_path,
    )

    assert (imported, remaining, migrated) == (0, 1, True)
    assert yaml.safe_load(queue_path.read_text())["matches"][0]["spotify_id"] == "sp-1"


def test_import_manual_matches_rejects_malformed_queue_before_writing_cache(tmp_path):
    queue_path = tmp_path / "songs_not_found.yml"
    queue_path.write_text("matches:\n  - not-a-mapping\n")

    with pytest.raises(ValueError, match="expected a mapping"):
        import_manual_matches(queue_path, tmp_path / ".cache.db")
