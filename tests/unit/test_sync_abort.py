import asyncio
from unittest.mock import MagicMock, patch

import pytest

from spotidal.errors import RateLimitAbortError, SyncAbortError
from spotidal.sync import search_new_tracks, sync_playlists_wrapper
from spotidal.type.models import Album, Artist, Playlist, Track


def test_sync_playlists_wrapper_stops_after_abort():
    source = MagicMock()
    dest = MagicMock()
    cache = MagicMock()
    failure_cache = MagicMock()
    config = {"rate_limit": 10, "max_concurrency": 10, "allow_deletions": False}
    playlists = [
        (Playlist(provider_id="1", name="First", description=""), None),
        (Playlist(provider_id="2", name="Second", description=""), None),
    ]

    def _abort_then_close(coro):
        coro.close()
        raise SyncAbortError("too long")

    with patch("spotidal.sync.asyncio.run", side_effect=_abort_then_close) as run_mock:
        completed = sync_playlists_wrapper(source, dest, playlists, cache, failure_cache, config)

    assert completed is False
    assert run_mock.call_count == 1


def test_search_new_tracks_cancels_pending_tasks_on_abort():
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()

    class Dest:
        name = "Spotify"

        async def search_track(self, track):
            if track.provider_id == "abort":
                started.set()
                raise RateLimitAbortError("too long")
            try:
                await started.wait()
                await release.wait()
                return None
            except asyncio.CancelledError:
                cancelled.set()
                raise

    tracks = [
        Track("abort", "Abort", [Artist("A")], Album("X", [Artist("A")]), None, 1.0, 1, None, True),
        Track("pending", "Pending", [Artist("B")], Album("Y", [Artist("B")]), None, 1.0, 1, None, True),
    ]
    cache = MagicMock()
    cache.get.return_value = None
    failure_cache = MagicMock()
    failure_cache.has_match_failure.return_value = False
    config = {"rate_limit": 10, "max_concurrency": 2, "allow_deletions": False}

    with pytest.raises(RateLimitAbortError):
        asyncio.run(search_new_tracks(Dest(), tracks, "Test", cache, failure_cache, config))

    assert cancelled.is_set()
