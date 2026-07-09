import asyncio
from collections.abc import Awaitable, Callable, Sequence
import datetime
from contextlib import suppress

from tqdm import tqdm

from spotidal.cache import MatchFailureDatabase, ProviderTrackDatabase, SyncSnapshotDatabase, TrackMatchCache
from spotidal.errors import SyncAbortError
from spotidal.match import match
from spotidal.manual_matches import add_not_found_match
from spotidal.providers.base import ReadProvider, ReadWriteProvider, WriteProvider
from spotidal.type.config import RuntimeConfig
from spotidal.type.models import Playlist, Track


def populate_track_match_cache(
    source_tracks_: Sequence[Track],
    dest_tracks_: Sequence[Track],
    cache: TrackMatchCache,
):
    """Populate the track match cache with existing destination tracks corresponding to source tracks."""
    def _populate_from_source(source_track: Track):
        for idx, dest_track in list(enumerate(dest_tracks)):
            if dest_track.available and match(dest_track, source_track):
                cache.insert(source_track.provider_id, dest_track.provider_id)
                dest_tracks.pop(idx)
                return

    def _populate_from_dest(dest_track: Track):
        for idx, source_track in list(enumerate(source_tracks)):
            if dest_track.available and match(dest_track, source_track):
                cache.insert(source_track.provider_id, dest_track.provider_id)
                source_tracks.pop(idx)
                return

    source_tracks = list(source_tracks_)
    dest_tracks = list(dest_tracks_)

    for track in dest_tracks:
        _populate_from_dest(track)
    for track in source_tracks:
        _populate_from_source(track)


def get_new_source_tracks(
    source_tracks: Sequence[Track],
    cache: TrackMatchCache,
    failure_cache: MatchFailureDatabase,
) -> list[Track]:
    """Extracts only the tracks that have not already been seen in our caches."""
    results = []
    for track in source_tracks:
        if not track.provider_id:
            continue
        if not cache.get(track.provider_id) and not failure_cache.has_match_failure(track.provider_id):
            results.append(track)
    return results


def get_dest_track_ids(
    source_tracks: Sequence[Track],
    cache: TrackMatchCache,
) -> list[str]:
    """Gets list of corresponding destination track ids for each source track, ignoring duplicates."""
    output = []
    seen_tracks: set[str] = set()

    for track in source_tracks:
        if not track.provider_id:
            continue
        dest_id = cache.get(track.provider_id)
        if dest_id:
            if dest_id in seen_tracks:
                track_name = track.name
                artist_names = ', '.join([a.name for a in track.artists])
                print(f'Duplicate found: Track "{track_name}" by {artist_names} will be ignored')
            else:
                output.append(dest_id)
                seen_tracks.add(dest_id)
    return output


def _provider_cache_key(provider: ReadProvider | WriteProvider) -> str:
    return provider.name.lower()


def _cache_provider_tracks(
    provider_cache: ProviderTrackDatabase | None,
    provider: ReadProvider | WriteProvider,
    tracks: Sequence[Track],
):
    if provider_cache is None or not tracks:
        return
    provider_cache.upsert_tracks(_provider_cache_key(provider), list(tracks))


async def search_new_tracks(
    dest: WriteProvider,
    source_tracks: Sequence[Track],
    playlist_name: str,
    cache: TrackMatchCache,
    failure_cache: MatchFailureDatabase,
    config: RuntimeConfig,
    provider_cache: ProviderTrackDatabase | None = None,
    source_provider_name: str | None = None,
):
    """Search for each source track on the destination provider and add results to the cache."""
    async def _run_rate_limiter(semaphore):
        """Leaky bucket algorithm for rate limiting. Periodically releases items from semaphore at rate_limit."""
        rate_limit = config.get('rate_limit', 10)
        _sleep_time = config.get('max_concurrency', 10) / rate_limit / 4
        t0 = datetime.datetime.now()
        accumulated = 0.0
        while True:
            await asyncio.sleep(_sleep_time)
            t = datetime.datetime.now()
            dt = (t - t0).total_seconds()
            t0 = t
            accumulated += rate_limit * dt
            new_items = int(accumulated)
            accumulated -= new_items
            for _ in range(new_items):
                semaphore.release()

    async def _rate_limited_search(track: Track, semaphore) -> Track | None:
        await semaphore.acquire()
        result = await dest.search_track(track)
        if result:
            failure_cache.remove_match_failure(track.provider_id)
        return result

    tracks_to_search = get_new_source_tracks(source_tracks, cache, failure_cache)
    if not tracks_to_search:
        return

    task_description = f"Searching {dest.name} for {len(tracks_to_search)}/{len(source_tracks)} tracks in playlist '{playlist_name}'"
    semaphore = asyncio.Semaphore(config.get('max_concurrency', 10))
    rate_limiter_task = asyncio.create_task(_run_rate_limiter(semaphore))
    search_tasks = [
        asyncio.create_task(_rate_limited_search(track, semaphore))
        for track in tracks_to_search
    ]
    search_results: list[Track | None] = [None] * len(tracks_to_search)

    try:
        with tqdm(total=len(search_tasks), desc=task_description) as progress:
            pending = set(search_tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    progress.update(1)
                    idx = search_tasks.index(task)
                    exc = task.exception()
                    if exc is not None:
                        for pending_task in pending:
                            pending_task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        raise exc
                    search_results[idx] = task.result()
    finally:
        rate_limiter_task.cancel()
        with suppress(asyncio.CancelledError):
            await rate_limiter_task

    song404 = []
    for idx, source_track in enumerate(tracks_to_search):
        if search_results[idx]:
            cache.insert(source_track.provider_id, search_results[idx].provider_id)
            _cache_provider_tracks(provider_cache, dest, [search_results[idx]])
        else:
            song404.append(f"{source_track.provider_id}: {','.join([a.name for a in source_track.artists])} - {source_track.name}")
            color = ('\033[91m', '\033[0m')
            print(color[0] + f"Could not find the track on {dest.name}: " + song404[-1] + color[1])
            failure_cache.cache_match_failure(source_track.provider_id)
    if song404:
        queue_path = config.get("not_found_path", "songs_not_found.yml")
        legacy_queue_path = config.get("legacy_not_found_path")
        source_provider = source_provider_name or ""
        for source_track in (track for track, result in zip(tracks_to_search, search_results) if result is None):
            add_not_found_match(
                queue_path, playlist_name, source_provider, dest.name, source_track, legacy_queue_path,
            )


async def sync_playlist(
    source: ReadProvider,
    dest: ReadWriteProvider,
    source_playlist: Playlist,
    dest_playlist: Playlist | None,
    cache: TrackMatchCache,
    failure_cache: MatchFailureDatabase,
    config: RuntimeConfig,
    provider_track_cache: ProviderTrackDatabase | None = None,
):
    """Sync a single playlist from source to destination."""
    source_tracks = await source.get_playlist_tracks(source_playlist)
    if not source_tracks:
        return
    _cache_provider_tracks(provider_track_cache, source, source_tracks)

    if dest_playlist:
        old_dest_tracks = await dest.get_playlist_tracks(dest_playlist)
        _cache_provider_tracks(provider_track_cache, dest, old_dest_tracks)
    else:
        print(f"No playlist found on {dest.name} corresponding to '{source_playlist.name}', creating new playlist")
        try:
            dest_playlist = await dest.create_playlist(source_playlist.name, source_playlist.description)
            failure_cache.remove_playlist_failure(source_playlist.provider_id)
        except Exception as e:
            print(f"Skipping '{source_playlist.name}': failed to create destination playlist — {e}")
            failure_cache.cache_playlist_failure(source_playlist.provider_id, source_playlist.name, str(e))
            return
        old_dest_tracks = []

    populate_track_match_cache(source_tracks, old_dest_tracks, cache)
    await search_new_tracks(
        dest, source_tracks, source_playlist.name, cache, failure_cache, config, provider_track_cache, source.name,
    )
    new_dest_track_ids = get_dest_track_ids(source_tracks, cache)

    old_dest_track_ids = [t.provider_id for t in old_dest_tracks]
    if new_dest_track_ids == old_dest_track_ids:
        print("No changes to write to playlist")
    elif config.get("allow_deletions"):
        await dest.clear_playlist(dest_playlist)
        await dest.add_tracks_to_playlist(dest_playlist, new_dest_track_ids)
    elif new_dest_track_ids[:len(old_dest_track_ids)] == old_dest_track_ids:
        await dest.add_tracks_to_playlist(dest_playlist, new_dest_track_ids[len(old_dest_track_ids):])
    else:
        await dest.clear_playlist(dest_playlist)
        await dest.add_tracks_to_playlist(dest_playlist, new_dest_track_ids)


async def sync_favorites(
    source: ReadProvider,
    dest: ReadWriteProvider,
    cache: TrackMatchCache,
    failure_cache: MatchFailureDatabase,
    config: RuntimeConfig,
    provider_track_cache: ProviderTrackDatabase | None = None,
):
    """Sync user favorites from source to destination."""
    source_tracks = await source.get_favorite_tracks()
    old_dest_tracks = await dest.get_favorite_tracks()
    _cache_provider_tracks(provider_track_cache, source, source_tracks)
    _cache_provider_tracks(provider_track_cache, dest, old_dest_tracks)

    populate_track_match_cache(source_tracks, old_dest_tracks, cache)
    await search_new_tracks(
        dest, source_tracks, "Favorites", cache, failure_cache, config, provider_track_cache, source.name,
    )

    existing_dest_ids = {t.provider_id for t in old_dest_tracks}
    new_ids = []
    for track in source_tracks:
        match_id = cache.get(track.provider_id)
        if match_id and match_id not in existing_dest_ids:
            new_ids.append(match_id)

    if new_ids:
        for dest_id in tqdm(new_ids, desc=f"Adding new tracks to {dest.name} favorites"):
            await dest.add_favorite_track(dest_id)
    else:
        print(f"No new tracks to add to {dest.name} favorites")


def sync_playlists_wrapper(
    source: ReadProvider,
    dest: ReadWriteProvider,
    playlists: list[tuple[Playlist, Playlist | None]],
    cache: TrackMatchCache,
    failure_cache: MatchFailureDatabase,
    config: RuntimeConfig,
    provider_track_cache: ProviderTrackDatabase | None = None,
):
    for source_playlist, dest_playlist in playlists:
        try:
            asyncio.run(
                sync_playlist(
                    source, dest, source_playlist, dest_playlist, cache, failure_cache, config, provider_track_cache,
                )
            )
        except SyncAbortError as e:
            print(f"{e}\nStopping sync early with partial progress preserved.")
            return False
    return True


def sync_favorites_wrapper(
    source: ReadProvider,
    dest: ReadWriteProvider,
    cache: TrackMatchCache,
    failure_cache: MatchFailureDatabase,
    config: RuntimeConfig,
    provider_track_cache: ProviderTrackDatabase | None = None,
):
    try:
        asyncio.run(
            sync_favorites(
                source=source, dest=dest, cache=cache, failure_cache=failure_cache,
                config=config, provider_track_cache=provider_track_cache,
            )
        )
    except SyncAbortError as e:
        print(f"{e}\nStopping sync early with partial progress preserved.")
        return False
    return True


# -- Bidirectional sync --

def _build_bidirectional_match_cache(
    tracks_a: Sequence[Track],
    tracks_b: Sequence[Track],
    persistent_cache_a_to_b: TrackMatchCache | None = None,
    persistent_cache_b_to_a: TrackMatchCache | None = None,
) -> tuple[TrackMatchCache, TrackMatchCache]:
    """Build two caches: a->b and b->a, using the same matching pass."""
    cache_a_to_b = TrackMatchCache()
    cache_b_to_a = TrackMatchCache()
    tracks_b_by_id = {track.provider_id: track for track in tracks_b}
    matched_a_ids: set[str] = set()
    matched_b_ids: set[str] = set()

    if persistent_cache_a_to_b is not None:
        for track_a in tracks_a:
            track_b_id = persistent_cache_a_to_b.get(track_a.provider_id)
            if track_b_id and track_b_id in tracks_b_by_id:
                cache_a_to_b.insert(track_a.provider_id, track_b_id)
                matched_a_ids.add(track_a.provider_id)
                matched_b_ids.add(track_b_id)

    if persistent_cache_b_to_a is not None:
        tracks_a_by_id = {track.provider_id: track for track in tracks_a}
        for track_b in tracks_b:
            if track_b.provider_id in matched_b_ids:
                continue
            track_a_id = persistent_cache_b_to_a.get(track_b.provider_id)
            if track_a_id and track_a_id in tracks_a_by_id and track_a_id not in matched_a_ids:
                cache_a_to_b.insert(track_a_id, track_b.provider_id)
                matched_a_ids.add(track_a_id)
                matched_b_ids.add(track_b.provider_id)

    populate_track_match_cache(
        [track for track in tracks_a if track.provider_id not in matched_a_ids],
        [track for track in tracks_b if track.provider_id not in matched_b_ids],
        cache_a_to_b,
    )
    for a_id, b_id in cache_a_to_b.data.items():
        cache_b_to_a.insert(b_id, a_id)
    return cache_a_to_b, cache_b_to_a


def playlist_snapshot_key(playlist_a: Playlist, playlist_b: Playlist) -> str:
    """Return a stable snapshot key that does not depend on editable playlist names."""
    return f"spotify:{playlist_a.provider_id}|tidal:{playlist_b.provider_id}"


async def rebuild_playlist_snapshot(
    provider_a: ReadProvider,
    provider_b: ReadProvider,
    playlist_a: Playlist | None,
    playlist_b: Playlist | None,
    snapshot_db: SyncSnapshotDatabase,
    persistent_cache_a_to_b: TrackMatchCache | None = None,
    persistent_cache_b_to_a: TrackMatchCache | None = None,
) -> tuple[str, int, int] | None:
    """Replace one snapshot from current local matches without mutating either provider."""
    if playlist_a is None or playlist_b is None:
        return None

    tracks_a = await provider_a.get_playlist_tracks(playlist_a)
    tracks_b = await provider_b.get_playlist_tracks(playlist_b)
    cache_a_to_b, _ = _build_bidirectional_match_cache(
        tracks_a, tracks_b, persistent_cache_a_to_b, persistent_cache_b_to_a,
    )
    pairs = cache_a_to_b.items()
    snapshot_db.save_snapshot(playlist_snapshot_key(playlist_a, playlist_b), pairs)
    matched_a_ids = {source_id for source_id, _ in pairs}
    matched_b_ids = {dest_id for _, dest_id in pairs}
    unmatched = sum(track.provider_id not in matched_a_ids for track in tracks_a)
    unmatched += sum(track.provider_id not in matched_b_ids for track in tracks_b)
    return playlist_a.name, len(pairs), unmatched


def _persist_match_pairs(
    cache_a_to_b: TrackMatchCache,
    persistent_cache_a_to_b: TrackMatchCache | None,
    persistent_cache_b_to_a: TrackMatchCache | None,
):
    """Mirror known pairs into the persistent directional caches."""
    if persistent_cache_a_to_b is None or persistent_cache_b_to_a is None:
        return

    for a_id, b_id in cache_a_to_b.data.items():
        persistent_cache_a_to_b.insert(a_id, b_id)
        persistent_cache_b_to_a.insert(b_id, a_id)


async def _sync_bidirectional_core(
    tracks_a: list[Track],
    tracks_b: list[Track],
    provider_a: ReadWriteProvider,
    provider_b: ReadWriteProvider,
    playlist_key: str,
    label: str,
    add_to_b: Callable[[list[str]], Awaitable[None]],
    add_to_a: Callable[[list[str]], Awaitable[None]],
    remove_from_a: Callable[[list[str]], Awaitable[None]],
    remove_from_b: Callable[[list[str]], Awaitable[None]],
    failure_cache: MatchFailureDatabase,
    snapshot_db: SyncSnapshotDatabase,
    config: RuntimeConfig,
    persistent_cache_a_to_b: TrackMatchCache | None = None,
    persistent_cache_b_to_a: TrackMatchCache | None = None,
    provider_track_cache: ProviderTrackDatabase | None = None,
):
    """Shared bidirectional sync logic: classify, search, add, delete, snapshot."""
    cache_a_to_b, cache_b_to_a = _build_bidirectional_match_cache(
        tracks_a, tracks_b, persistent_cache_a_to_b, persistent_cache_b_to_a,
    )
    _persist_match_pairs(cache_a_to_b, persistent_cache_a_to_b, persistent_cache_b_to_a)

    previous_snapshot = snapshot_db.get_snapshot(playlist_key)
    previous_a_ids = {pair[0] for pair in previous_snapshot}
    previous_b_ids = {pair[1] for pair in previous_snapshot}

    only_on_a = [t for t in tracks_a if not cache_a_to_b.get(t.provider_id)]
    only_on_b = [t for t in tracks_b if not cache_b_to_a.get(t.provider_id)]

    # Classify: new addition vs deletion from other side
    to_add_to_b: list[Track] = []
    to_delete_from_a: list[str] = []
    for track in only_on_a:
        if track.provider_id in previous_a_ids:
            to_delete_from_a.append(track.provider_id)
        else:
            to_add_to_b.append(track)

    to_add_to_a: list[Track] = []
    to_delete_from_b: list[str] = []
    for track in only_on_b:
        if track.provider_id in previous_b_ids:
            to_delete_from_b.append(track.provider_id)
        else:
            to_add_to_a.append(track)

    # Search and add missing tracks
    if to_add_to_b:
        search_cache_a_to_b = persistent_cache_a_to_b or cache_a_to_b
        await search_new_tracks(
            provider_b, to_add_to_b, label, search_cache_a_to_b, failure_cache, config, provider_track_cache,
            provider_a.name,
        )
        new_b_ids = []
        for track in to_add_to_b:
            b_id = search_cache_a_to_b.get(track.provider_id)
            if not b_id:
                continue
            cache_a_to_b.insert(track.provider_id, b_id)
            cache_b_to_a.insert(b_id, track.provider_id)
            if persistent_cache_b_to_a is not None:
                persistent_cache_b_to_a.insert(b_id, track.provider_id)
            new_b_ids.append(b_id)
        if new_b_ids:
            await add_to_b(new_b_ids)

    if to_add_to_a:
        search_cache_b_to_a = persistent_cache_b_to_a or cache_b_to_a
        await search_new_tracks(
            provider_a, to_add_to_a, label, search_cache_b_to_a, failure_cache, config, provider_track_cache,
            provider_b.name,
        )
        new_a_ids = []
        for track in to_add_to_a:
            a_id = search_cache_b_to_a.get(track.provider_id)
            if not a_id:
                continue
            cache_b_to_a.insert(track.provider_id, a_id)
            cache_a_to_b.insert(a_id, track.provider_id)
            if persistent_cache_a_to_b is not None:
                persistent_cache_a_to_b.insert(a_id, track.provider_id)
            new_a_ids.append(a_id)
        if new_a_ids:
            await add_to_a(new_a_ids)

    # Apply deletions
    if to_delete_from_a or to_delete_from_b:
        if config.get('allow_deletions'):
            if to_delete_from_a:
                print(f"Removing {len(to_delete_from_a)} deleted track(s) from {provider_a.name} {label}")
                await remove_from_a(to_delete_from_a)
            if to_delete_from_b:
                print(f"Removing {len(to_delete_from_b)} deleted track(s) from {provider_b.name} {label}")
                await remove_from_b(to_delete_from_b)
        else:
            total = len(to_delete_from_a) + len(to_delete_from_b)
            print(f"Skipping removal of {total} track(s) - enable allow_deletions in config to allow")
            # Preserve snapshot pairs so skipped deletions aren't re-added next run
            prev_a_to_b = {a: b for a, b in previous_snapshot}
            prev_b_to_a = {b: a for a, b in previous_snapshot}
            for a_id in to_delete_from_a:
                if a_id in prev_a_to_b:
                    cache_a_to_b.insert(a_id, prev_a_to_b[a_id])
            for b_id in to_delete_from_b:
                if b_id in prev_b_to_a:
                    cache_a_to_b.insert(prev_b_to_a[b_id], b_id)

    if not to_add_to_b and not to_add_to_a and not to_delete_from_a and not to_delete_from_b:
        print(f"No changes to {label} on either side")

    # Save snapshot of all currently matched pairs
    current_pairs = list(cache_a_to_b.data.items())
    snapshot_db.save_snapshot(playlist_key, current_pairs)


async def sync_playlist_bidirectional(
    provider_a: ReadWriteProvider,
    provider_b: ReadWriteProvider,
    playlist_a: Playlist | None,
    playlist_b: Playlist | None,
    failure_cache: MatchFailureDatabase,
    snapshot_db: SyncSnapshotDatabase,
    config: RuntimeConfig,
    persistent_cache_a_to_b: TrackMatchCache | None = None,
    persistent_cache_b_to_a: TrackMatchCache | None = None,
    provider_track_cache: ProviderTrackDatabase | None = None,
):
    """Bidirectional sync: add missing tracks to each side, detect and propagate deletions."""
    if not playlist_a and not playlist_b:
        print("Warning: both playlists are None, skipping")
        return

    if playlist_a:
        tracks_a = await provider_a.get_playlist_tracks(playlist_a)
        _cache_provider_tracks(provider_track_cache, provider_a, tracks_a)
    else:
        print(f"No playlist found on {provider_a.name} corresponding to '{playlist_b.name}', creating new playlist")
        playlist_a = await provider_a.create_playlist(playlist_b.name, playlist_b.description)
        tracks_a = []

    if playlist_b:
        tracks_b = await provider_b.get_playlist_tracks(playlist_b)
        _cache_provider_tracks(provider_track_cache, provider_b, tracks_b)
    else:
        print(f"No playlist found on {provider_b.name} corresponding to '{playlist_a.name}', creating new playlist")
        playlist_b = await provider_b.create_playlist(playlist_a.name, playlist_a.description)
        tracks_b = []

    await _sync_bidirectional_core(
        tracks_a, tracks_b, provider_a, provider_b,
        playlist_key=playlist_snapshot_key(playlist_a, playlist_b),
        label=f"playlist '{playlist_a.name}'",
        add_to_b=lambda ids: provider_b.add_tracks_to_playlist(playlist_b, ids),
        add_to_a=lambda ids: provider_a.add_tracks_to_playlist(playlist_a, ids),
        remove_from_a=lambda ids: provider_a.remove_tracks_from_playlist(playlist_a, ids),
        remove_from_b=lambda ids: provider_b.remove_tracks_from_playlist(playlist_b, ids),
        failure_cache=failure_cache, snapshot_db=snapshot_db, config=config,
        persistent_cache_a_to_b=persistent_cache_a_to_b,
        persistent_cache_b_to_a=persistent_cache_b_to_a,
        provider_track_cache=provider_track_cache,
    )


async def sync_favorites_bidirectional(
    provider_a: ReadWriteProvider,
    provider_b: ReadWriteProvider,
    failure_cache: MatchFailureDatabase,
    snapshot_db: SyncSnapshotDatabase,
    config: RuntimeConfig,
    persistent_cache_a_to_b: TrackMatchCache | None = None,
    persistent_cache_b_to_a: TrackMatchCache | None = None,
    provider_track_cache: ProviderTrackDatabase | None = None,
):
    """Bidirectional sync of favorites."""
    tracks_a = await provider_a.get_favorite_tracks()
    tracks_b = await provider_b.get_favorite_tracks()
    _cache_provider_tracks(provider_track_cache, provider_a, tracks_a)
    _cache_provider_tracks(provider_track_cache, provider_b, tracks_b)

    async def _add_favorites(provider: ReadWriteProvider, ids: list[str]):
        for tid in ids:
            await provider.add_favorite_track(tid)

    async def _remove_favorites(provider: ReadWriteProvider, ids: list[str]):
        for tid in ids:
            await provider.remove_favorite_track(tid)

    await _sync_bidirectional_core(
        tracks_a, tracks_b, provider_a, provider_b,
        playlist_key="__favorites__",
        label="favorites",
        add_to_b=lambda ids: _add_favorites(provider_b, ids),
        add_to_a=lambda ids: _add_favorites(provider_a, ids),
        remove_from_a=lambda ids: _remove_favorites(provider_a, ids),
        remove_from_b=lambda ids: _remove_favorites(provider_b, ids),
        failure_cache=failure_cache, snapshot_db=snapshot_db, config=config,
        persistent_cache_a_to_b=persistent_cache_a_to_b,
        persistent_cache_b_to_a=persistent_cache_b_to_a,
        provider_track_cache=provider_track_cache,
    )


def sync_playlists_bidirectional_wrapper(
    provider_a: ReadWriteProvider,
    provider_b: ReadWriteProvider,
    playlists: list[tuple[Playlist, Playlist | None]],
    failure_cache: MatchFailureDatabase,
    snapshot_db: SyncSnapshotDatabase,
    config: RuntimeConfig,
    persistent_cache_a_to_b: TrackMatchCache | None = None,
    persistent_cache_b_to_a: TrackMatchCache | None = None,
    provider_track_cache: ProviderTrackDatabase | None = None,
):
    for playlist_a, playlist_b in playlists:
        try:
            asyncio.run(
                sync_playlist_bidirectional(
                    provider_a, provider_b, playlist_a, playlist_b, failure_cache, snapshot_db, config,
                    persistent_cache_a_to_b, persistent_cache_b_to_a, provider_track_cache,
                )
            )
        except SyncAbortError as e:
            print(f"{e}\nStopping sync early with partial progress preserved.")
            return False
    return True


def sync_favorites_bidirectional_wrapper(
    provider_a: ReadWriteProvider,
    provider_b: ReadWriteProvider,
    failure_cache: MatchFailureDatabase,
    snapshot_db: SyncSnapshotDatabase,
    config: RuntimeConfig,
    persistent_cache_a_to_b: TrackMatchCache | None = None,
    persistent_cache_b_to_a: TrackMatchCache | None = None,
    provider_track_cache: ProviderTrackDatabase | None = None,
):
    try:
        asyncio.run(
            sync_favorites_bidirectional(
                provider_a, provider_b, failure_cache, snapshot_db, config,
                persistent_cache_a_to_b, persistent_cache_b_to_a, provider_track_cache,
            )
        )
    except SyncAbortError as e:
        print(f"{e}\nStopping sync early with partial progress preserved.")
        return False
    return True
