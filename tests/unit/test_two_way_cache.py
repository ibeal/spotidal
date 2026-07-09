from unittest.mock import AsyncMock, MagicMock

import pytest

from spotidal.cache import TrackMatchCache
from spotidal.errors import SyncAbortError
from spotidal.run import _execute_sync
from spotidal.sync import _sync_bidirectional_core, playlist_snapshot_key, rebuild_playlist_snapshot
from spotidal.type.models import Album, Artist, Playlist, Track


def test_two_way_sync_uses_persistent_cache_to_skip_lookup():
    spotify_to_tidal = TrackMatchCache()
    tidal_to_spotify = TrackMatchCache()
    spotify_to_tidal.insert("sp-1", "td-1")

    provider_a = MagicMock()
    provider_a.name = "Spotify"
    provider_a.search_track = AsyncMock()
    provider_b = MagicMock()
    provider_b.name = "Tidal"
    provider_b.search_track = AsyncMock()

    add_to_b = AsyncMock()
    add_to_a = AsyncMock()
    remove_from_a = AsyncMock()
    remove_from_b = AsyncMock()
    failure_cache = MagicMock()
    snapshot_db = MagicMock()
    snapshot_db.get_snapshot.return_value = set()

    track_a = Track(
        "sp-1", "Song", [Artist("Artist")], Album("Album", [Artist("Artist")]),
        None, 180.0, 1, None, True,
    )

    import asyncio
    asyncio.run(
        _sync_bidirectional_core(
            tracks_a=[track_a],
            tracks_b=[],
            provider_a=provider_a,
            provider_b=provider_b,
            playlist_key="playlist",
            label="playlist 'playlist'",
            add_to_b=add_to_b,
            add_to_a=add_to_a,
            remove_from_a=remove_from_a,
            remove_from_b=remove_from_b,
            failure_cache=failure_cache,
            snapshot_db=snapshot_db,
            config={"allow_deletions": False, "max_concurrency": 2, "rate_limit": 2},
            persistent_cache_a_to_b=spotify_to_tidal,
            persistent_cache_b_to_a=tidal_to_spotify,
        )
    )

    provider_b.search_track.assert_not_called()
    add_to_b.assert_awaited_once_with(["td-1"])
    assert tidal_to_spotify.get("td-1") == "sp-1"


def test_two_way_sync_uses_imported_pair_already_present_on_both_sides():
    spotify_to_tidal = TrackMatchCache()
    tidal_to_spotify = TrackMatchCache()
    spotify_to_tidal.insert("sp-1", "td-1")
    tidal_to_spotify.insert("td-1", "sp-1")

    provider_a = MagicMock(name="Spotify")
    provider_a.name = "Spotify"
    provider_b = MagicMock(name="Tidal")
    provider_b.name = "Tidal"
    add_to_b = AsyncMock()
    add_to_a = AsyncMock()
    snapshot_db = MagicMock()
    snapshot_db.get_snapshot.return_value = set()

    track_a = Track("sp-1", "Spotify title", [Artist("Artist")], Album("A", [Artist("Artist")]), None, 180.0, 1, None, True)
    track_b = Track("td-1", "Tidal title", [Artist("Different")], Album("B", [Artist("Different")]), None, 240.0, 1, None, True)

    import asyncio
    asyncio.run(
        _sync_bidirectional_core(
            tracks_a=[track_a], tracks_b=[track_b], provider_a=provider_a, provider_b=provider_b,
            playlist_key="playlist", label="playlist 'playlist'", add_to_b=add_to_b, add_to_a=add_to_a,
            remove_from_a=AsyncMock(), remove_from_b=AsyncMock(), failure_cache=MagicMock(),
            snapshot_db=snapshot_db, config={"allow_deletions": False, "max_concurrency": 2, "rate_limit": 2},
            persistent_cache_a_to_b=spotify_to_tidal, persistent_cache_b_to_a=tidal_to_spotify,
        )
    )

    add_to_a.assert_not_awaited()
    add_to_b.assert_not_awaited()


def test_rebuild_playlist_snapshot_writes_only_current_pairs():
    spotify = MagicMock()
    spotify.get_playlist_tracks = AsyncMock(return_value=[
        Track("sp-1", "Song", [Artist("Artist")], Album("A", [Artist("Artist")]), None, 180.0, 1, None, True),
        Track("sp-2", "Unmatched", [Artist("Artist")], Album("A", [Artist("Artist")]), None, 180.0, 1, None, True),
    ])
    tidal = MagicMock()
    tidal.get_playlist_tracks = AsyncMock(return_value=[
        Track("td-1", "Song", [Artist("Artist")], Album("A", [Artist("Artist")]), None, 180.0, 1, None, True),
    ])
    spotify_playlist = Playlist("sp-playlist", "Playlist", "")
    tidal_playlist = Playlist("td-playlist", "Playlist", "")
    snapshot_db = MagicMock()

    import asyncio
    result = asyncio.run(rebuild_playlist_snapshot(
        spotify, tidal, spotify_playlist, tidal_playlist, snapshot_db,
    ))

    assert result == ("Playlist", 1, 1)
    snapshot_db.save_snapshot.assert_called_once_with(
        "spotify:sp-playlist|tidal:td-playlist", [("sp-1", "td-1")],
    )
    assert playlist_snapshot_key(spotify_playlist, tidal_playlist) == "spotify:sp-playlist|tidal:td-playlist"
    assert not spotify.method_calls[1:]
    assert not tidal.method_calls[1:]


def test_rebuild_playlist_snapshot_uses_tidal_to_spotify_associations():
    spotify = MagicMock()
    spotify.get_playlist_tracks = AsyncMock(return_value=[
        Track("sp-1", "Different Spotify title", [Artist("A")], Album("A", [Artist("A")]), None, 1.0, 1, None, True),
    ])
    tidal = MagicMock()
    tidal.get_playlist_tracks = AsyncMock(return_value=[
        Track("td-1", "Different Tidal title", [Artist("B")], Album("B", [Artist("B")]), None, 2.0, 1, None, True),
    ])
    tidal_to_spotify = TrackMatchCache()
    tidal_to_spotify.insert("td-1", "sp-1")
    spotify_playlist = Playlist("sp-playlist", "Playlist", "")
    tidal_playlist = Playlist("td-playlist", "Playlist", "")
    snapshot_db = MagicMock()

    import asyncio
    result = asyncio.run(rebuild_playlist_snapshot(
        spotify, tidal, spotify_playlist, tidal_playlist, snapshot_db,
        persistent_cache_b_to_a=tidal_to_spotify,
    ))

    assert result == ("Playlist", 1, 0)


def test_two_way_sync_requires_rebuild_when_legacy_snapshot_exists(mocker):
    snapshot_db = MagicMock()
    snapshot_db.has_snapshots.return_value = True
    mocker.patch("spotidal.run.SyncSnapshotDatabase", return_value=snapshot_db)
    spotify_playlist = Playlist("sp-playlist", "Playlist", "")
    tidal_playlist = Playlist("td-playlist", "Playlist", "")

    with pytest.raises(SyncAbortError, match="--rebuild-snapshots"):
        _execute_sync(
            mode="two-way", direction="spotify-to-tidal", favorites=False,
            spotify=MagicMock(), tidal=MagicMock(), pairs=[(spotify_playlist, tidal_playlist)],
            failure_cache=MagicMock(), provider_track_cache=MagicMock(),
            runtime_config={"allow_deletions": False, "max_concurrency": 1, "rate_limit": 1},
            config_path="config.yml",
        )
