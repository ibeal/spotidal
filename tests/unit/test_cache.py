# tests/unit/test_cache.py

import pytest
from sqlalchemy import create_engine, select
from spotidal.cache import MatchFailureDatabase, ProviderTrackDatabase, SyncSnapshotDatabase, TrackAssociationDatabase, TrackMatchCache
from spotidal.type.models import Album, Artist, Track


# Setup an in-memory SQLite database for testing
@pytest.fixture
def in_memory_db():
    engine = create_engine("sqlite:///:memory:")
    return engine


# Test MatchFailureDatabase
def test_cache_match_failure(in_memory_db, mocker):
    mocker.patch(
        "spotidal.cache.sqlalchemy.create_engine", return_value=in_memory_db
    )
    failure_db = MatchFailureDatabase()

    track_id = "test_track"
    failure_db.cache_match_failure(track_id)

    with failure_db.engine.connect() as connection:
        result = connection.execute(
            select(failure_db.match_failures).where(
                failure_db.match_failures.c.track_id == track_id
            )
        ).fetchone()
        assert result is not None
        assert result.track_id == track_id


def test_has_match_failure(in_memory_db, mocker):
    mocker.patch(
        "spotidal.cache.sqlalchemy.create_engine", return_value=in_memory_db
    )
    failure_db = MatchFailureDatabase()

    track_id = "test_track"
    failure_db.cache_match_failure(track_id)

    assert failure_db.has_match_failure(track_id) is True


def test_remove_match_failure(in_memory_db, mocker):
    mocker.patch(
        "spotidal.cache.sqlalchemy.create_engine", return_value=in_memory_db
    )
    failure_db = MatchFailureDatabase()

    track_id = "test_track"
    failure_db.cache_match_failure(track_id)
    failure_db.remove_match_failure(track_id)

    with failure_db.engine.connect() as connection:
        result = connection.execute(
            select(failure_db.match_failures).where(
                failure_db.match_failures.c.track_id == track_id
            )
        ).fetchone()
        assert result is None


# Test TrackMatchCache
def test_track_match_cache_insert():
    track_cache = TrackMatchCache()
    track_cache.insert("spotify_id", "123")
    assert track_cache.get("spotify_id") == "123"


def test_track_match_cache_get():
    track_cache = TrackMatchCache()
    track_cache.insert("spotify_id", "123")
    assert track_cache.get("spotify_id") == "123"
    assert track_cache.get("nonexistent_id") is None


def test_provider_track_database_round_trip(in_memory_db, mocker):
    mocker.patch(
        "spotidal.cache.sqlalchemy.create_engine", return_value=in_memory_db
    )
    provider_db = ProviderTrackDatabase()
    track = Track(
        "sp-1", "Song", [Artist("Artist")], Album("Album", [Artist("Artist")]),
        "ISRC1", 180.0, 1, "Deluxe", True,
    )

    provider_db.upsert_track("spotify", track)
    cached = provider_db.get_track("spotify", "sp-1")

    assert cached == track


def test_track_match_cache_persists_provider_specific_associations(in_memory_db, mocker):
    mocker.patch(
        "spotidal.cache.sqlalchemy.create_engine", return_value=in_memory_db
    )
    spotify_to_tidal = TrackMatchCache(
        filename=".cache.db", source_provider="spotify", dest_provider="tidal"
    )
    tidal_to_spotify = TrackMatchCache(
        filename=".cache.db", source_provider="tidal", dest_provider="spotify"
    )

    spotify_to_tidal.insert("123", "abc")
    tidal_to_spotify.insert("123", "xyz")

    assert spotify_to_tidal.get("123") == "abc"
    assert tidal_to_spotify.get("123") == "xyz"


def test_track_match_cache_migrates_legacy_rows(in_memory_db, mocker):
    mocker.patch(
        "spotidal.cache.sqlalchemy.create_engine", return_value=in_memory_db
    )
    association_db = TrackAssociationDatabase()
    with association_db.engine.connect() as connection:
        with connection.begin():
            connection.execute(
                association_db.legacy_track_matches.insert(),
                [
                    {"source_id": "sp-plain", "dest_id": "td-plain"},
                    {"source_id": "spotify->tidal:sp-namespaced", "dest_id": "td-namespaced"},
                ],
            )

    plain_cache = TrackMatchCache(
        filename=".cache.db", source_provider="spotify", dest_provider="tidal"
    )
    namespaced_cache = TrackMatchCache(
        filename=".cache.db",
        source_provider="spotify",
        dest_provider="tidal",
        legacy_prefix="spotify->tidal",
    )

    plain_cache.migrate_legacy_entries()
    namespaced_cache.migrate_legacy_entries()

    assert plain_cache.get("sp-plain") == "td-plain"
    assert namespaced_cache.get("sp-namespaced") == "td-namespaced"


def test_snapshot_database_detects_and_removes_legacy_snapshot(in_memory_db, mocker):
    mocker.patch("spotidal.cache.sqlalchemy.create_engine", return_value=in_memory_db)
    snapshots = SyncSnapshotDatabase()
    snapshots.save_snapshot("Playlist", [("sp-1", "td-1")])

    assert snapshots.has_snapshots({"Playlist"}) is True
    snapshots.delete_snapshot("Playlist")

    assert snapshots.has_snapshots({"Playlist"}) is False
