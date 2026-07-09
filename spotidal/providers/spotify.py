import asyncio
import email.utils
import math
import time
from collections.abc import Callable
from typing import TypeVar

import requests
import spotipy
from tqdm import tqdm
from tqdm.asyncio import tqdm as atqdm
from urllib3.util.retry import Retry

from spotidal.errors import AuthenticationError, RateLimitAbortError
from spotidal.match import match, simple
from spotidal.type.models import Album, Artist, Playlist, Track

SPOTIFY_SCOPES = 'playlist-read-private, playlist-modify-private, playlist-modify-public, user-library-read, user-library-modify'

# Spotify's default of 3 retries is easily exhausted during a 429 storm; spotipy's
# session already honors the Retry-After header, so a larger budget just lets it wait it out.
SPOTIFY_RETRIES = 10
DEFAULT_MAX_WAIT_FOR_RATE_LIMIT_S = 3600

T = TypeVar("T")


class RetryAfterLimitExceeded(Exception):
    """Raised before sleeping when Spotify asks us to wait longer than allowed."""

    def __init__(self, retry_after_s: int, max_wait_s: int):
        self.retry_after_s = retry_after_s
        self.max_wait_s = max_wait_s
        super().__init__(
            f"Spotify requested a retry delay of {retry_after_s}s, exceeding max_wait_for_rate_limit={max_wait_s}s"
        )


class BoundedRetry(Retry):
    """Retry policy that refuses Retry-After delays above the configured bound."""

    @staticmethod
    def _get_raw_retry_after(response) -> int | None:
        if response is None:
            return None

        retry_after = response.headers.get("Retry-After")
        if retry_after is None:
            return None

        retry_after = retry_after.strip()
        if retry_after.isdigit():
            return int(retry_after)

        retry_date_tuple = email.utils.parsedate_tz(retry_after)
        if retry_date_tuple is None:
            return None

        retry_date = email.utils.mktime_tz(retry_date_tuple)
        return max(0, int(retry_date - time.time()))

    def sleep(self, response=None):
        raw_retry_after = self._get_raw_retry_after(response)
        if raw_retry_after is not None and raw_retry_after > self.retry_after_max:
            print(
                "Spotify rate limit requested retry after "
                f"{raw_retry_after}s, exceeding max_wait_for_rate_limit={int(self.retry_after_max)}s; aborting run"
            )
            raise RetryAfterLimitExceeded(raw_retry_after, int(self.retry_after_max))
        return super().sleep(response)

    def sleep_for_retry(self, response) -> bool:
        raw_retry_after = self._get_raw_retry_after(response)
        if raw_retry_after:
            print(f"Spotify rate limit hit; waiting {raw_retry_after}s before retrying")
            return super().sleep_for_retry(response)
        return False


class SpotifyProvider:
    def __init__(
        self,
        session: spotipy.Spotify,
        max_concurrency: int = 5,
        max_wait_for_rate_limit_s: int = DEFAULT_MAX_WAIT_FOR_RATE_LIMIT_S,
    ):
        self._session = session
        self._user_id: str | None = None
        # Caps concurrent paginated fetches so we don't burst dozens of requests at once.
        self._max_concurrency = max(1, max_concurrency)
        self._max_wait_for_rate_limit_s = max(1, max_wait_for_rate_limit_s)

    @classmethod
    def from_config(
        cls,
        config: dict,
        max_concurrency: int = 5,
        max_wait_for_rate_limit_s: int = DEFAULT_MAX_WAIT_FOR_RATE_LIMIT_S,
    ) -> 'SpotifyProvider':
        print("Opening Spotify session")
        credentials_manager = spotipy.SpotifyOAuth(
            username=config['username'],
            scope=SPOTIFY_SCOPES,
            client_id=config['client_id'],
            client_secret=config['client_secret'],
            redirect_uri=config['redirect_uri'],
            requests_timeout=2,
            open_browser=config.get('open_browser', True),
        )
        try:
            credentials_manager.get_access_token(as_dict=False)
        except spotipy.SpotifyOauthError as e:
            raise AuthenticationError(f"Error opening Spotify session; could not get token for username: {config['username']}") from e
        requests_session = requests.Session()
        retry = BoundedRetry(
            total=SPOTIFY_RETRIES,
            connect=None,
            read=False,
            allowed_methods=frozenset(['GET', 'POST', 'PUT', 'DELETE']),
            status=SPOTIFY_RETRIES,
            backoff_factor=0.3,
            status_forcelist=spotipy.Spotify.default_retry_codes,
            retry_after_max=max_wait_for_rate_limit_s,
        )
        adapter = requests.adapters.HTTPAdapter(max_retries=retry)
        requests_session.mount('http://', adapter)
        requests_session.mount('https://', adapter)
        session = spotipy.Spotify(
            oauth_manager=credentials_manager,
            requests_session=requests_session,
        )
        return cls(
            session,
            max_concurrency=max_concurrency,
            max_wait_for_rate_limit_s=max_wait_for_rate_limit_s,
        )

    @property
    def name(self) -> str:
        return 'Spotify'

    def _call_with_rate_limit_guard(self, fn: Callable[[], T]) -> T:
        try:
            return fn()
        except RetryAfterLimitExceeded as e:
            raise RateLimitAbortError(
                f"{e}. Aborting this sync run early so the next scheduled run can continue from a partial sync."
            ) from e

    def _get_user_id(self) -> str:
        if self._user_id is None:
            self._user_id = self._call_with_rate_limit_guard(lambda: self._session.current_user())['id']
        return self._user_id

    @staticmethod
    def _normalize_track(raw: dict) -> Track:
        album_raw = raw.get('album', {})
        return Track(
            provider_id=raw['id'],
            name=raw['name'],
            artists=[Artist(name=a['name']) for a in raw.get('artists', [])],
            album=Album(
                name=album_raw.get('name', ''),
                artists=[Artist(name=a['name']) for a in album_raw.get('artists', [])],
            ),
            isrc=raw.get('external_ids', {}).get('isrc'),
            duration_s=raw['duration_ms'] / 1000,
            track_number=raw.get('track_number', 0),
            version=None,
            available=True,
        )

    @staticmethod
    def _normalize_playlist(raw: dict) -> Playlist:
        return Playlist(
            provider_id=raw['id'],
            name=raw['name'],
            description=raw.get('description', ''),
        )

    async def _gather_pages(self, fetch_function, offsets: list[int], desc: str | None = None) -> list[dict]:
        """Fetch pages concurrently, but cap in-flight requests to avoid tripping rate limits."""
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def _fetch(offset: int):
            async with semaphore:
                return await asyncio.to_thread(lambda: self._call_with_rate_limit_guard(lambda: fetch_function(offset)))

        return await atqdm.gather(*[_fetch(offset) for offset in offsets], desc=desc)

    async def _fetch_all_paginated(self, fetch_function, item_key: str = 'track') -> list[dict]:
        output = []
        results = self._call_with_rate_limit_guard(lambda: fetch_function(0))
        output.extend([item[item_key] for item in results['items'] if item.get(item_key) is not None])

        if results['next']:
            offsets = [results['limit'] * n for n in range(1, math.ceil(results['total'] / results['limit']))]
            extra_results = await self._gather_pages(fetch_function, offsets, desc="Fetching additional data chunks")
            for extra_result in extra_results:
                output.extend([item[item_key] for item in extra_result['items'] if item.get(item_key) is not None])

        return output

    async def get_playlists(self, exclude_ids: set[str] | None = None) -> list[Playlist]:
        exclude_ids = exclude_ids or set()
        user_id = self._get_user_id()

        playlists = []
        print("Loading Spotify playlists")
        first_results = self._call_with_rate_limit_guard(self._session.current_user_playlists)
        playlists.extend(first_results['items'])

        if first_results['next']:
            offsets = [first_results['limit'] * n for n in range(1, math.ceil(first_results['total'] / first_results['limit']))]
            extra_results = await self._gather_pages(
                lambda offset: self._session.current_user_playlists(offset=offset), offsets
            )
            for extra_result in extra_results:
                playlists.extend(extra_result['items'])

        return [
            self._normalize_playlist(p) for p in playlists
            if p and p['owner']['id'] == user_id and p['id'] not in exclude_ids
        ]

    async def get_playlist_tracks(self, playlist: Playlist) -> list[Track]:
        def _fetch(offset: int):
            return self._session.playlist_tracks(playlist_id=playlist.provider_id, offset=offset)

        print(f"Loading tracks from Spotify playlist '{playlist.name}'")
        raw_tracks = await self._fetch_all_paginated(_fetch, item_key='item')

        def _is_valid(item: dict) -> bool:
            return (
                item.get('type', 'track') == 'track'
                and 'album' in item
                and 'name' in item['album']
                and 'artists' in item['album']
                and len(item['album']['artists']) > 0
                and item['album']['artists'][0]['name'] is not None
            )

        return [self._normalize_track(t) for t in raw_tracks if _is_valid(t)]

    async def get_favorite_tracks(self) -> list[Track]:
        def _fetch(offset: int):
            return self._session.current_user_saved_tracks(offset=offset)

        print("Loading favorite tracks from Spotify")
        raw_tracks = await self._fetch_all_paginated(_fetch)
        raw_tracks.reverse()
        return [self._normalize_track(t) for t in raw_tracks]

    async def get_playlist_by_id(self, playlist_id: str) -> Playlist:
        raw = self._call_with_rate_limit_guard(lambda: self._session.playlist(playlist_id=playlist_id))
        return self._normalize_playlist(raw)

    # -- WriteProvider implementation --

    async def search_track(self, source_track: Track) -> Track | None:
        """Search Spotify for a track matching the source track."""
        def _search():
            if not source_track.artists:
                return None
            query = simple(source_track.name) + ' ' + simple(source_track.artists[0].name)
            results = self._call_with_rate_limit_guard(lambda: self._session.search(q=query, type='track', limit=10))
            for item in results['tracks']['items']:
                normalized = self._normalize_track(item)
                if match(normalized, source_track):
                    return normalized
            return None

        try:
            return await asyncio.to_thread(_search)
        except RateLimitAbortError:
            raise
        except Exception as e:
            print(f"Error searching Spotify for '{source_track.name}': {e}")
            return None

    async def create_playlist(self, name: str, description: str) -> Playlist:
        try:
            raw = self._call_with_rate_limit_guard(
                lambda: self._session.user_playlist_create(
                    self._get_user_id(), name, public=False, description=description
                )
            )
        except spotipy.SpotifyException as e:
            if e.http_status == 403:
                raise AuthenticationError(
                    "Spotify returned 403 creating a playlist — your token is missing "
                    "'playlist-modify-private' / 'playlist-modify-public' scopes. "
                    "Delete the cached token and re-authenticate."
                ) from e
            raise
        return self._normalize_playlist(raw)

    async def add_tracks_to_playlist(self, playlist: Playlist, track_ids: list[str]) -> None:
        uris = [f'spotify:track:{tid}' for tid in track_ids]
        offset = 0
        chunk_size = 100
        with tqdm(desc="Adding new tracks to Spotify playlist", total=len(uris)) as progress:
            while offset < len(uris):
                count = min(chunk_size, len(uris) - offset)
                self._call_with_rate_limit_guard(
                    lambda: self._session.playlist_add_items(playlist.provider_id, uris[offset:offset + chunk_size])
                )
                offset += count
                progress.update(count)

    async def clear_playlist(self, playlist: Playlist) -> None:
        self._call_with_rate_limit_guard(lambda: self._session.playlist_replace_items(playlist.provider_id, []))

    async def add_favorite_track(self, track_id: str) -> None:
        self._call_with_rate_limit_guard(lambda: self._session.current_user_saved_tracks_add(tracks=[track_id]))

    async def remove_tracks_from_playlist(self, playlist: Playlist, track_ids: list[str]) -> None:
        uris = [f'spotify:track:{tid}' for tid in track_ids]
        # Spotify allows removing up to 100 tracks per call
        for i in range(0, len(uris), 100):
            self._call_with_rate_limit_guard(
                lambda: self._session.playlist_remove_all_occurrences_of_items(playlist.provider_id, uris[i:i + 100])
            )

    async def remove_favorite_track(self, track_id: str) -> None:
        self._call_with_rate_limit_guard(lambda: self._session.current_user_saved_tracks_delete(tracks=[track_id]))
