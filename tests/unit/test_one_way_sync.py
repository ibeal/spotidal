import asyncio
from unittest.mock import AsyncMock, MagicMock

from spotidal.cache import TrackMatchCache
from spotidal.sync import sync_playlist
from spotidal.type.models import Album, Artist, Playlist, Track


def _track(provider_id: str, name: str) -> Track:
    return Track(
        provider_id=provider_id,
        name=name,
        artists=[Artist("Artist")],
        album=Album("Album", [Artist("Artist")]),
        isrc=None,
        duration_s=180.0,
        track_number=1,
        version=None,
        available=True,
    )


def test_one_way_sync_appends_when_destination_is_prefix_and_deletions_disabled():
    source = MagicMock()
    source.name = "Tidal"
    source.get_playlist_tracks = AsyncMock(return_value=[
        _track("td-1", "Song One"),
        _track("td-2", "Song Two"),
        _track("td-3", "Song Three"),
    ])

    dest = MagicMock()
    dest.name = "Spotify"
    dest.get_playlist_tracks = AsyncMock(return_value=[
        _track("sp-1", "Song One"),
        _track("sp-2", "Song Two"),
    ])
    dest.search_track = AsyncMock(return_value=_track("sp-3", "Song Three"))
    dest.clear_playlist = AsyncMock()
    dest.add_tracks_to_playlist = AsyncMock()
    failure_cache = MagicMock()
    failure_cache.has_match_failure.return_value = False

    asyncio.run(
        sync_playlist(
            source=source,
            dest=dest,
            source_playlist=Playlist("src", "Playlist", ""),
            dest_playlist=Playlist("dst", "Playlist", ""),
            cache=TrackMatchCache(),
            failure_cache=failure_cache,
            config={"allow_deletions": False, "max_concurrency": 2, "rate_limit": 2},
        )
    )

    dest.clear_playlist.assert_not_awaited()
    dest.add_tracks_to_playlist.assert_awaited_once_with(
        Playlist("dst", "Playlist", ""),
        ["sp-3"],
    )


def test_one_way_sync_rewrites_playlist_when_deletions_enabled():
    source = MagicMock()
    source.name = "Tidal"
    source.get_playlist_tracks = AsyncMock(return_value=[
        _track("td-1", "Song One"),
        _track("td-2", "Song Two"),
        _track("td-3", "Song Three"),
    ])

    dest = MagicMock()
    dest.name = "Spotify"
    dest.get_playlist_tracks = AsyncMock(return_value=[
        _track("sp-1", "Song One"),
        _track("sp-2", "Song Two"),
    ])
    dest.search_track = AsyncMock(return_value=_track("sp-3", "Song Three"))
    dest.clear_playlist = AsyncMock()
    dest.add_tracks_to_playlist = AsyncMock()
    failure_cache = MagicMock()
    failure_cache.has_match_failure.return_value = False

    asyncio.run(
        sync_playlist(
            source=source,
            dest=dest,
            source_playlist=Playlist("src", "Playlist", ""),
            dest_playlist=Playlist("dst", "Playlist", ""),
            cache=TrackMatchCache(),
            failure_cache=failure_cache,
            config={"allow_deletions": True, "max_concurrency": 2, "rate_limit": 2},
        )
    )

    dest.clear_playlist.assert_awaited_once()
    dest.add_tracks_to_playlist.assert_awaited_once_with(
        Playlist("dst", "Playlist", ""),
        ["sp-1", "sp-2", "sp-3"],
    )
