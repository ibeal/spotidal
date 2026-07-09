# tests/unit/test_auth.py

from unittest.mock import MagicMock

import asyncio

import pytest
import spotipy
from spotidal.errors import AuthenticationError, RateLimitAbortError
from spotidal.providers.spotify import (
    BoundedRetry,
    DEFAULT_MAX_WAIT_FOR_RATE_LIMIT_S,
    RetryAfterLimitExceeded,
    SpotifyProvider,
    SPOTIFY_RETRIES,
    SPOTIFY_SCOPES,
)
from spotidal.type.models import Playlist


def test_open_spotify_session(mocker):
    mock_spotify_oauth = mocker.patch(
        "spotidal.providers.spotify.spotipy.SpotifyOAuth", autospec=True
    )
    mock_spotify_instance = mocker.patch(
        "spotidal.providers.spotify.spotipy.Spotify", autospec=True
    )
    mock_session_cls = mocker.patch(
        "spotidal.providers.spotify.requests.Session", autospec=True
    )
    mock_adapter_cls = mocker.patch(
        "spotidal.providers.spotify.requests.adapters.HTTPAdapter", autospec=True
    )

    mock_config = {
        "username": "test_user",
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
        "redirect_uri": "http://127.0.0.1/",
        "open_browser": True,
    }

    mock_oauth_instance = mock_spotify_oauth.return_value
    mock_oauth_instance.get_access_token.return_value = "mock_access_token"
    mock_requests_session = mock_session_cls.return_value

    provider = SpotifyProvider.from_config(mock_config)

    mock_spotify_oauth.assert_called_once_with(
        username="test_user",
        scope=SPOTIFY_SCOPES,
        client_id="test_client_id",
        client_secret="test_client_secret",
        redirect_uri="http://127.0.0.1/",
        requests_timeout=2,
        open_browser=True,
    )

    mock_adapter_cls.assert_called_once()
    retry = mock_adapter_cls.call_args.kwargs["max_retries"]
    assert isinstance(retry, BoundedRetry)
    assert retry.total == SPOTIFY_RETRIES
    assert retry.status == SPOTIFY_RETRIES
    assert retry.retry_after_max == DEFAULT_MAX_WAIT_FOR_RATE_LIMIT_S
    mock_requests_session.mount.assert_any_call("http://", mock_adapter_cls.return_value)
    mock_requests_session.mount.assert_any_call("https://", mock_adapter_cls.return_value)

    mock_spotify_instance.assert_called_once_with(
        oauth_manager=mock_oauth_instance,
        requests_session=mock_requests_session,
    )
    assert provider._session == mock_spotify_instance.return_value


def test_open_spotify_session_oauth_error(mocker):
    mock_spotify_oauth = mocker.patch(
        "spotidal.providers.spotify.spotipy.SpotifyOAuth", autospec=True
    )
    mock_spotify_oauth.return_value.get_access_token.side_effect = (
        spotipy.SpotifyOauthError("mock error")
    )

    mock_config = {
        "username": "test_user",
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
        "redirect_uri": "http://127.0.0.1/",
    }

    with pytest.raises(AuthenticationError):
        SpotifyProvider.from_config(mock_config)


def test_rate_limit_abort_wraps_excessive_retry_after(mocker):
    provider = SpotifyProvider(mocker.Mock(spec=spotipy.Spotify))

    with pytest.raises(RateLimitAbortError) as exc_info:
        provider._call_with_rate_limit_guard(
            lambda: (_ for _ in ()).throw(RetryAfterLimitExceeded(86306, 3600))
        )

    assert "partial sync" in str(exc_info.value)


def test_bounded_retry_uses_raw_retry_after_header():
    retry = BoundedRetry(total=1, retry_after_max=3600)
    response = MagicMock()
    response.headers = {"Retry-After": "86306"}

    with pytest.raises(RetryAfterLimitExceeded) as exc_info:
        retry.sleep(response)

    assert exc_info.value.retry_after_s == 86306
    assert exc_info.value.max_wait_s == 3600


def test_get_playlist_tracks_uses_current_spotify_item_shape(mocker):
    session = mocker.Mock()
    session.playlist_tracks.return_value = {
        "items": [{"item": {
            "id": "sp-1", "name": "Song", "type": "track", "duration_ms": 180000,
            "track_number": 1, "external_ids": {"isrc": "ISRC"},
            "artists": [{"name": "Artist"}],
            "album": {"name": "Album", "artists": [{"name": "Artist"}]},
        }}],
        "next": None, "total": 1, "limit": 100,
    }
    provider = SpotifyProvider(session)

    tracks = asyncio.run(provider.get_playlist_tracks(Playlist("playlist", "Playlist", "")))

    assert [track.provider_id for track in tracks] == ["sp-1"]
