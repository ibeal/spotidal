import asyncio
import os

from spotidal import sync as _sync
from spotidal.cache import MatchFailureDatabase, ProviderTrackDatabase, SyncSnapshotDatabase, TrackMatchCache
from spotidal.config import backfill_playlist_ids, build_runtime_config, save_config
from spotidal.errors import SyncAbortError
from spotidal.providers.spotify import SpotifyProvider
from spotidal.providers.tidal import TidalProvider
from spotidal.type.config import AppConfig, PlaylistEntry, RuntimeConfig


def _data_path(config_path: str, filename: str) -> str:
    """Derive data file paths from the config file's directory."""
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), filename)


def _execute_sync(
    mode: str,
    direction: str,
    favorites: bool,
    spotify: SpotifyProvider,
    tidal: TidalProvider,
    pairs: list[tuple],
    failure_cache: MatchFailureDatabase,
    provider_track_cache: ProviderTrackDatabase,
    runtime_config: RuntimeConfig,
    config_path: str,
):
    """Shared sync orchestration for both run_sync and run_oneshot."""
    runtime_config["not_found_path"] = _data_path(config_path, "songs_not_found.yml")
    runtime_config["legacy_not_found_path"] = os.path.abspath("songs_not_found.txt")
    if mode == "two-way":
        snapshot_db = SyncSnapshotDatabase(_data_path(config_path, ".cache.db"))
        legacy_snapshot_names = {
            spotify_playlist.name
            for spotify_playlist, tidal_playlist in pairs
            if spotify_playlist is not None and tidal_playlist is not None
        }
        if snapshot_db.has_snapshots(legacy_snapshot_names):
            raise SyncAbortError(
                "Legacy name-keyed snapshots exist for selected playlists. "
                "Run `spotidal --rebuild-snapshots` before another two-way sync."
            )
        spotify_to_tidal_cache = TrackMatchCache(
            _data_path(config_path, ".cache.db"),
            source_provider="spotify",
            dest_provider="tidal",
            legacy_prefix="spotify->tidal",
        )
        tidal_to_spotify_cache = TrackMatchCache(
            _data_path(config_path, ".cache.db"),
            source_provider="tidal",
            dest_provider="spotify",
            legacy_prefix="tidal->spotify",
        )
        spotify_to_tidal_cache.migrate_legacy_entries()
        tidal_to_spotify_cache.migrate_legacy_entries()
        if pairs:
            completed = _sync.sync_playlists_bidirectional_wrapper(
                spotify, tidal, pairs, failure_cache, snapshot_db, runtime_config,
                spotify_to_tidal_cache, tidal_to_spotify_cache, provider_track_cache,
            )
            if not completed:
                return False
        if favorites:
            return _sync.sync_favorites_bidirectional_wrapper(
                spotify, tidal, failure_cache, snapshot_db, runtime_config,
                spotify_to_tidal_cache, tidal_to_spotify_cache, provider_track_cache,
            )
        return True
    else:
        if direction == "tidal-to-spotify":
            source, dest = tidal, spotify
            pairs = [(td, sp) for sp, td in pairs]
            source_provider = "tidal"
            dest_provider = "spotify"
        else:
            source, dest = spotify, tidal
            source_provider = "spotify"
            dest_provider = "tidal"
        cache = TrackMatchCache(
            _data_path(config_path, ".cache.db"),
            source_provider=source_provider,
            dest_provider=dest_provider,
        )
        cache.migrate_legacy_entries()
        if pairs:
            completed = _sync.sync_playlists_wrapper(
                source, dest, pairs, cache, failure_cache, runtime_config, provider_track_cache,
            )
            if not completed:
                return False
        if favorites:
            return _sync.sync_favorites_wrapper(
                source, dest, cache, failure_cache, runtime_config, provider_track_cache,
            )
        return True


def run_sync(config: AppConfig, config_path: str):
    """Execute sync based on saved configuration (non-interactive)."""
    spotify = SpotifyProvider.from_config(
        config["spotify"],
        max_concurrency=config.get("max_concurrency", 5),
        max_wait_for_rate_limit_s=config.get("max_wait_for_rate_limit", 3600),
    )
    tidal = TidalProvider.from_config(_data_path(config_path, ".session.yml"))
    failure_cache = MatchFailureDatabase(_data_path(config_path, ".cache.db"))
    provider_track_cache = ProviderTrackDatabase(_data_path(config_path, ".cache.db"))
    runtime_config = build_runtime_config(config)
    sync_config = config["sync"]
    try:
        pairs = _build_playlist_pairs(config["sync"]["playlists"], spotify, tidal)
    except SyncAbortError as e:
        print(f"{e}\nStopping sync early with partial progress preserved.")
        return False

    completed = _execute_sync(
        mode=sync_config["mode"],
        direction=sync_config["direction"],
        favorites=sync_config["favorites"],
        spotify=spotify, tidal=tidal, pairs=pairs,
        failure_cache=failure_cache, provider_track_cache=provider_track_cache, runtime_config=runtime_config,
        config_path=config_path,
    )

    if completed:
        backfill_playlist_ids(config, spotify, tidal, config_path)
    return completed


def run_rebuild_snapshots(config: AppConfig, config_path: str):
    """Rebuild configured playlist snapshots without searching or changing either provider."""
    spotify = SpotifyProvider.from_config(
        config["spotify"],
        max_concurrency=config.get("max_concurrency", 5),
        max_wait_for_rate_limit_s=config.get("max_wait_for_rate_limit", 3600),
    )
    tidal = TidalProvider.from_config(_data_path(config_path, ".session.yml"))
    pairs = _build_playlist_pairs(config["sync"]["playlists"], spotify, tidal)
    cache_path = _data_path(config_path, ".cache.db")
    snapshot_db = SyncSnapshotDatabase(cache_path)
    spotify_to_tidal_cache = TrackMatchCache(
        cache_path, source_provider="spotify", dest_provider="tidal", legacy_prefix="spotify->tidal",
    )
    tidal_to_spotify_cache = TrackMatchCache(
        cache_path, source_provider="tidal", dest_provider="spotify", legacy_prefix="tidal->spotify",
    )
    spotify_to_tidal_cache.migrate_legacy_entries()
    tidal_to_spotify_cache.migrate_legacy_entries()

    for spotify_playlist, tidal_playlist in pairs:
        result = asyncio.run(
            _sync.rebuild_playlist_snapshot(
                spotify, tidal, spotify_playlist, tidal_playlist, snapshot_db,
                spotify_to_tidal_cache, tidal_to_spotify_cache,
            )
        )
        if result is None:
            name = (spotify_playlist or tidal_playlist).name
            print(f"Skipping '{name}': both Spotify and Tidal playlist IDs are required for a snapshot.")
            continue
        name, matched, unmatched = result
        snapshot_db.delete_snapshot(name)
        print(f"Rebuilt snapshot for '{name}': {matched} matched pair(s), {unmatched} unmatched track(s).")


def run_oneshot(config: AppConfig | None, config_path: str):
    """Interactive one-shot sync: prompt for everything, run once, don't save sync selections."""
    from spotidal.setup import (
        authenticate_spotify,
        authenticate_tidal,
        prompt_allow_deletions,
        prompt_direction,
        prompt_favorites,
        prompt_playlists,
        prompt_spotify_credentials,
        prompt_sync_mode,
    )

    # Auth: use existing creds or prompt and persist them
    if config and config.get("spotify"):
        spotify_config = config["spotify"]
    else:
        spotify_config = prompt_spotify_credentials(None)

    spotify = authenticate_spotify(spotify_config)
    tidal = authenticate_tidal(_data_path(config_path, ".session.yml"))

    # Persist creds if they weren't saved yet
    if not config or not config.get("spotify"):
        if not config:
            config = {
                "config_version": 2,
                "spotify": spotify_config,
                "sync": {
                    "mode": "two-way",
                    "direction": "spotify-to-tidal",
                    "favorites": True,
                    "allow_deletions": False,
                    "playlists": [],
                },
                "max_concurrency": 10,
                "rate_limit": 10,
                "max_wait_for_rate_limit": 3600,
            }
        else:
            config["spotify"] = spotify_config
        save_config(config, config_path)

    # Prompt sync choices (ephemeral - not saved to config)
    mode = prompt_sync_mode(None)
    direction = prompt_direction(None) if mode == "one-way" else "spotify-to-tidal"
    playlists = prompt_playlists(spotify, tidal, mode, direction, [])
    favorites = prompt_favorites(None)
    allow_deletions = prompt_allow_deletions(None)

    runtime_config = build_runtime_config(config)
    runtime_config["allow_deletions"] = allow_deletions
    failure_cache = MatchFailureDatabase(_data_path(config_path, ".cache.db"))
    provider_track_cache = ProviderTrackDatabase(_data_path(config_path, ".cache.db"))
    try:
        pairs = _build_playlist_pairs(playlists, spotify, tidal)
    except SyncAbortError as e:
        print(f"{e}\nStopping sync early with partial progress preserved.")
        return

    _execute_sync(
        mode=mode, direction=direction, favorites=favorites,
        spotify=spotify, tidal=tidal, pairs=pairs,
        failure_cache=failure_cache, provider_track_cache=provider_track_cache, runtime_config=runtime_config,
        config_path=config_path,
    )


def _build_playlist_pairs(
    entries: list[PlaylistEntry],
    spotify: SpotifyProvider,
    tidal: TidalProvider,
) -> list[tuple]:
    """Resolve playlist entries into (SpotifyPlaylist, TidalPlaylist|None) pairs."""
    pairs = []
    for entry in entries:
        spotify_id = entry.get("spotify_id")
        tidal_id = entry.get("tidal_id")

        try:
            sp_playlist = asyncio.run(spotify.get_playlist_by_id(spotify_id)) if spotify_id else None
            td_playlist = asyncio.run(tidal.get_playlist_by_id(tidal_id)) if tidal_id else None
        except Exception as e:
            name = entry.get("name", spotify_id or tidal_id)
            print(f"Warning: could not load playlist '{name}': {e}. Skipping.")
            continue

        pairs.append((sp_playlist, td_playlist))

    return pairs
