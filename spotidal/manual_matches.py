from pathlib import Path
import re

import yaml

from spotidal.cache import MatchFailureDatabase, TrackMatchCache
from spotidal.type.models import Track


QUEUE_KEY = "matches"
LEGACY_HEADER_RE = re.compile(
    r"^=== Songs not found on playlist (?P<playlist>.+) on (?P<destination>Spotify|Tidal) ===$"
)


def _queue_path(path: str | Path) -> Path:
    return Path(path)


def _read_queue(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    if not isinstance(data, dict) or not isinstance(data.get(QUEUE_KEY, []), list):
        raise ValueError(f"Invalid manual match queue: {path}")
    entries = data.get(QUEUE_KEY, [])
    required_text_fields = ("playlist", "artist", "title")
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid manual match queue entry {index}: expected a mapping")
        if any(not isinstance(entry.get(field), str) for field in required_text_fields):
            raise ValueError(f"Invalid manual match queue entry {index}: missing playlist, artist, or title")
        if any(entry.get(field) is not None and not isinstance(entry.get(field), str) for field in ("tidal_id", "spotify_id")):
            raise ValueError(f"Invalid manual match queue entry {index}: IDs must be strings or null")
    return entries


def _write_queue(path: Path, entries: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as file:
        yaml.safe_dump({QUEUE_KEY: entries}, file, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _migrate_legacy_queue(path: Path, legacy_path: str | Path | None = None) -> bool:
    """Create the YAML queue from the old text log once, retaining the source file."""
    legacy = Path(legacy_path) if legacy_path else path.with_suffix(".txt")
    if path.exists() or not legacy.exists():
        return False

    entries: list[dict] = []
    playlist: str | None = None
    destination: str | None = None
    for line in legacy.read_text(encoding="utf-8").splitlines():
        if header := LEGACY_HEADER_RE.match(line):
            playlist = header.group("playlist")
            destination = header.group("destination").lower()
            continue
        if not line or playlist is None or destination is None or ": " not in line:
            continue
        source_id, metadata = line.split(": ", 1)
        artist, separator, title = metadata.partition(" - ")
        if not separator:
            continue
        entries.append({
            "playlist": playlist,
            "artist": artist,
            "title": title,
            "tidal_id": source_id if destination == "spotify" else None,
            "spotify_id": source_id if destination == "tidal" else None,
        })

    _write_queue(path, entries)
    return True


def add_not_found_match(
    queue_path: str | Path,
    playlist: str,
    source_provider: str,
    destination_provider: str,
    source_track: Track,
    legacy_queue_path: str | Path | None = None,
) -> None:
    """Append an unresolved track to the editable queue unless it is already present."""
    path = _queue_path(queue_path)
    _migrate_legacy_queue(path, legacy_queue_path)
    entries = _read_queue(path)
    source_provider = source_provider.lower()
    destination_provider = destination_provider.lower()
    if {source_provider, destination_provider} != {"spotify", "tidal"}:
        raise ValueError("Manual matches require Spotify and Tidal providers")

    entry = {
        "playlist": playlist,
        "artist": ", ".join(artist.name for artist in source_track.artists),
        "title": source_track.name,
        "tidal_id": source_track.provider_id if source_provider == "tidal" else None,
        "spotify_id": source_track.provider_id if source_provider == "spotify" else None,
    }
    if entry not in entries:
        entries.append(entry)
        _write_queue(path, entries)


def import_manual_matches(
    queue_path: str | Path,
    cache_path: str | Path,
    legacy_queue_path: str | Path | None = None,
) -> tuple[int, int, bool]:
    """Import completed queue rows into both directional caches without provider API calls."""
    path = _queue_path(queue_path)
    migrated = _migrate_legacy_queue(path, legacy_queue_path)
    entries = _read_queue(path)
    tidal_to_spotify = TrackMatchCache(
        str(cache_path), source_provider="tidal", dest_provider="spotify",
    )
    spotify_to_tidal = TrackMatchCache(
        str(cache_path), source_provider="spotify", dest_provider="tidal",
    )
    failure_cache = MatchFailureDatabase(str(cache_path))
    remaining = []
    imported = 0

    for entry in entries:
        tidal_id = entry.get("tidal_id")
        spotify_id = entry.get("spotify_id")
        if not isinstance(tidal_id, str) or not tidal_id or not isinstance(spotify_id, str) or not spotify_id:
            remaining.append(entry)
            continue
        tidal_to_spotify.insert(tidal_id, spotify_id)
        spotify_to_tidal.insert(spotify_id, tidal_id)
        failure_cache.remove_match_failure(tidal_id)
        failure_cache.remove_match_failure(spotify_id)
        imported += 1

    if imported:
        _write_queue(path, remaining)
    return imported, len(remaining), migrated
