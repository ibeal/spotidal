import datetime

import sqlalchemy
from sqlalchemy import Column, DateTime, Float, Integer, MetaData, String, Table, delete, insert, select, update

from spotidal.type.models import Album, Artist, Track


class MatchFailureDatabase:
    """
    SQLite database of match failures which persists between runs.
    This can be used concurrently between multiple processes.
    """

    def __init__(self, filename='.cache.db'):
        self.engine = sqlalchemy.create_engine(f"sqlite:///{filename}")
        meta = MetaData()
        self.match_failures = Table('match_failures', meta,
                                    Column('track_id', String,
                                           primary_key=True),
                                    Column('insert_time', DateTime),
                                    Column('next_retry', DateTime),
                                    sqlite_autoincrement=False)
        self.playlist_failures = Table('playlist_failures', meta,
                                       Column('playlist_id', String, primary_key=True),
                                       Column('playlist_name', String),
                                       Column('error', String),
                                       Column('insert_time', DateTime),
                                       sqlite_autoincrement=False)
        meta.create_all(self.engine)

    def _get_next_retry_time(self, insert_time: datetime.datetime | None = None) -> datetime.datetime:
        if insert_time:
            # double interval on each retry
            interval = 2 * (datetime.datetime.now() - insert_time)
        else:
            interval = datetime.timedelta(days=7)
        return datetime.datetime.now() + interval

    def cache_match_failure(self, track_id: str):
        """Notifies that matching failed for the given track_id."""
        fetch_statement = select(self.match_failures).where(
            self.match_failures.c.track_id == track_id)
        with self.engine.connect() as connection:
            with connection.begin():
                existing_failure = connection.execute(
                    fetch_statement).fetchone()
                if existing_failure:
                    update_statement = update(self.match_failures).where(
                        self.match_failures.c.track_id == track_id).values(next_retry=self._get_next_retry_time())
                    connection.execute(update_statement)
                else:
                    connection.execute(insert(self.match_failures), {
                                       "track_id": track_id, "insert_time": datetime.datetime.now(), "next_retry": self._get_next_retry_time()})

    def has_match_failure(self, track_id: str) -> bool:
        """Checks if there was a recent search for which matching failed with the given track_id."""
        statement = select(self.match_failures.c.next_retry).where(
            self.match_failures.c.track_id == track_id)
        with self.engine.connect() as connection:
            match_failure = connection.execute(statement).fetchone()
            if match_failure:
                return match_failure.next_retry > datetime.datetime.now()
            return False

    def remove_match_failure(self, track_id: str):
        """Removes match failure from the database."""
        statement = delete(self.match_failures).where(
            self.match_failures.c.track_id == track_id)
        with self.engine.connect() as connection:
            with connection.begin():
                connection.execute(statement)

    def cache_playlist_failure(self, playlist_id: str, playlist_name: str, error: str):
        """Records that creating/syncing a playlist failed."""
        with self.engine.connect() as connection:
            with connection.begin():
                existing = connection.execute(
                    select(self.playlist_failures).where(
                        self.playlist_failures.c.playlist_id == playlist_id)
                ).fetchone()
                if existing:
                    connection.execute(
                        update(self.playlist_failures)
                        .where(self.playlist_failures.c.playlist_id == playlist_id)
                        .values(playlist_name=playlist_name, error=error, insert_time=datetime.datetime.now()))
                else:
                    connection.execute(insert(self.playlist_failures), {
                        "playlist_id": playlist_id,
                        "playlist_name": playlist_name,
                        "error": error,
                        "insert_time": datetime.datetime.now(),
                    })

    def get_playlist_failures(self) -> list[dict]:
        """Returns all recorded playlist failures."""
        with self.engine.connect() as connection:
            rows = connection.execute(select(self.playlist_failures)).fetchall()
            return [{"playlist_id": r.playlist_id, "playlist_name": r.playlist_name,
                     "error": r.error, "insert_time": r.insert_time} for r in rows]

    def remove_playlist_failure(self, playlist_id: str):
        """Removes a playlist failure record (e.g. after successful retry)."""
        with self.engine.connect() as connection:
            with connection.begin():
                connection.execute(
                    delete(self.playlist_failures).where(
                        self.playlist_failures.c.playlist_id == playlist_id))


class SyncSnapshotDatabase:
    """
    Persists the set of matched track pairs after each bidirectional sync run.
    Used to detect deletions: if a pair was in the previous snapshot but one side
    is now missing, the track was deleted from that side.
    """

    def __init__(self, filename='.cache.db'):
        self.engine = sqlalchemy.create_engine(f"sqlite:///{filename}")
        meta = MetaData()
        self.sync_snapshots = Table('sync_snapshots', meta,
                                    Column('playlist_key', String, primary_key=True),
                                    Column('provider_a_id', String, primary_key=True),
                                    Column('provider_b_id', String, primary_key=True),
                                    Column('last_seen', DateTime))
        meta.create_all(self.engine)

    def save_snapshot(self, playlist_key: str, pairs: list[tuple[str, str]]):
        """Replace all entries for this playlist with current matched pairs."""
        with self.engine.connect() as connection:
            with connection.begin():
                connection.execute(
                    delete(self.sync_snapshots).where(
                        self.sync_snapshots.c.playlist_key == playlist_key))
                if pairs:
                    connection.execute(
                        insert(self.sync_snapshots),
                        [{"playlist_key": playlist_key, "provider_a_id": a, "provider_b_id": b,
                          "last_seen": datetime.datetime.now()} for a, b in pairs])

    def get_snapshot(self, playlist_key: str) -> set[tuple[str, str]]:
        """Get previous snapshot as set of (provider_a_id, provider_b_id) pairs."""
        statement = select(
            self.sync_snapshots.c.provider_a_id,
            self.sync_snapshots.c.provider_b_id,
        ).where(self.sync_snapshots.c.playlist_key == playlist_key)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).fetchall()
            return {(row.provider_a_id, row.provider_b_id) for row in rows}

    def has_snapshots(self, playlist_keys: set[str]) -> bool:
        """Return whether any of the given snapshot keys still have stored pairs."""
        if not playlist_keys:
            return False
        statement = select(self.sync_snapshots.c.playlist_key).where(
            self.sync_snapshots.c.playlist_key.in_(playlist_keys)
        ).limit(1)
        with self.engine.connect() as connection:
            return connection.execute(statement).fetchone() is not None

    def delete_snapshot(self, playlist_key: str):
        """Remove a superseded snapshot after its replacement has been written."""
        with self.engine.connect() as connection:
            with connection.begin():
                connection.execute(
                    delete(self.sync_snapshots).where(
                        self.sync_snapshots.c.playlist_key == playlist_key)
                )


class ProviderTrackDatabase:
    """Persistent normalized metadata keyed by provider and track id."""

    def __init__(self, filename='.cache.db'):
        self.engine = sqlalchemy.create_engine(f"sqlite:///{filename}")
        meta = MetaData()
        self.provider_tracks = Table(
            'provider_tracks', meta,
            Column('provider', String, primary_key=True),
            Column('track_id', String, primary_key=True),
            Column('name', String),
            Column('artist_names', String),
            Column('album_name', String),
            Column('album_artist_names', String),
            Column('isrc', String),
            Column('duration_s', Float),
            Column('track_number', Integer),
            Column('version', String),
            Column('available', Integer),
            Column('last_seen', DateTime),
        )
        meta.create_all(self.engine)

    @staticmethod
    def _serialize_names(artists: list[Artist]) -> str:
        return '|||'.join(artist.name for artist in artists)

    @staticmethod
    def _deserialize_names(raw: str | None) -> list[Artist]:
        if not raw:
            return []
        return [Artist(name=name) for name in raw.split('|||') if name]

    def upsert_track(self, provider: str, track: Track):
        values = {
            "provider": provider,
            "track_id": track.provider_id,
            "name": track.name,
            "artist_names": self._serialize_names(track.artists),
            "album_name": track.album.name,
            "album_artist_names": self._serialize_names(track.album.artists),
            "isrc": track.isrc,
            "duration_s": track.duration_s,
            "track_number": track.track_number,
            "version": track.version,
            "available": 1 if track.available else 0,
            "last_seen": datetime.datetime.now(),
        }
        with self.engine.connect() as connection:
            with connection.begin():
                existing = connection.execute(
                    select(self.provider_tracks.c.track_id).where(
                        self.provider_tracks.c.provider == provider,
                        self.provider_tracks.c.track_id == track.provider_id,
                    )
                ).fetchone()
                if existing:
                    connection.execute(
                        update(self.provider_tracks).where(
                            self.provider_tracks.c.provider == provider,
                            self.provider_tracks.c.track_id == track.provider_id,
                        ).values(**values)
                    )
                else:
                    connection.execute(insert(self.provider_tracks), values)

    def upsert_tracks(self, provider: str, tracks: list[Track]):
        for track in tracks:
            self.upsert_track(provider, track)

    def get_track(self, provider: str, track_id: str) -> Track | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(self.provider_tracks).where(
                    self.provider_tracks.c.provider == provider,
                    self.provider_tracks.c.track_id == track_id,
                )
            ).fetchone()
            if row is None:
                return None
            return Track(
                provider_id=row.track_id,
                name=row.name,
                artists=self._deserialize_names(row.artist_names),
                album=Album(
                    name=row.album_name or '',
                    artists=self._deserialize_names(row.album_artist_names),
                ),
                isrc=row.isrc,
                duration_s=row.duration_s or 0.0,
                track_number=row.track_number or 0,
                version=row.version,
                available=bool(row.available),
            )


class TrackAssociationDatabase:
    """Persistent provider-aware cross-service associations, with legacy migration support."""

    def __init__(self, filename='.cache.db'):
        self.engine = sqlalchemy.create_engine(f"sqlite:///{filename}")
        meta = MetaData()
        self.track_associations = Table(
            'track_associations', meta,
            Column('source_provider', String, primary_key=True),
            Column('source_id', String, primary_key=True),
            Column('dest_provider', String, primary_key=True),
            Column('dest_id', String),
            Column('last_seen', DateTime),
        )
        self.legacy_track_matches = Table(
            'track_matches', meta,
            Column('source_id', String, primary_key=True),
            Column('dest_id', String),
            extend_existing=True,
        )
        meta.create_all(self.engine)

    def get(self, source_provider: str, source_id: str, dest_provider: str) -> str | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(self.track_associations.c.dest_id).where(
                    self.track_associations.c.source_provider == source_provider,
                    self.track_associations.c.source_id == source_id,
                    self.track_associations.c.dest_provider == dest_provider,
                )
            ).fetchone()
            return row.dest_id if row else None

    def upsert(self, source_provider: str, source_id: str, dest_provider: str, dest_id: str):
        values = {
            "source_provider": source_provider,
            "source_id": source_id,
            "dest_provider": dest_provider,
            "dest_id": dest_id,
            "last_seen": datetime.datetime.now(),
        }
        with self.engine.connect() as connection:
            with connection.begin():
                existing = connection.execute(
                    select(self.track_associations.c.source_id).where(
                        self.track_associations.c.source_provider == source_provider,
                        self.track_associations.c.source_id == source_id,
                        self.track_associations.c.dest_provider == dest_provider,
                    )
                ).fetchone()
                if existing:
                    connection.execute(
                        update(self.track_associations).where(
                            self.track_associations.c.source_provider == source_provider,
                            self.track_associations.c.source_id == source_id,
                            self.track_associations.c.dest_provider == dest_provider,
                        ).values(**values)
                    )
                else:
                    connection.execute(insert(self.track_associations), values)

    def items(self, source_provider: str, dest_provider: str) -> list[tuple[str, str]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(self.track_associations.c.source_id, self.track_associations.c.dest_id).where(
                    self.track_associations.c.source_provider == source_provider,
                    self.track_associations.c.dest_provider == dest_provider,
                )
            ).fetchall()
            return [(row.source_id, row.dest_id) for row in rows]

    def get_legacy_match(self, key: str) -> str | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(self.legacy_track_matches.c.dest_id).where(
                    self.legacy_track_matches.c.source_id == key
                )
            ).fetchone()
            return row.dest_id if row else None

    def legacy_items(self, prefix: str | None = None) -> list[tuple[str, str]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(self.legacy_track_matches.c.source_id, self.legacy_track_matches.c.dest_id)
            ).fetchall()
            output = []
            for row in rows:
                if prefix is None:
                    if ':' in row.source_id:
                        continue
                    output.append((row.source_id, row.dest_id))
                elif row.source_id.startswith(f"{prefix}:"):
                    output.append((row.source_id.removeprefix(f"{prefix}:"), row.dest_id))
            return output


class TrackMatchCache:
    """
    Mapping of source track ids -> destination track ids.
    Pass a filename and provider pair to persist in the new provider-aware table.
    Legacy track_matches rows are bulk-copied on demand so old caches remain useful.
    """

    def __init__(
        self,
        filename: str | None = None,
        source_provider: str | None = None,
        dest_provider: str | None = None,
        legacy_prefix: str | None = None,
    ):
        self._filename = filename
        self._source_provider = source_provider
        self._dest_provider = dest_provider
        self._legacy_prefix = legacy_prefix
        self._mem: dict[str, str] = {}
        self._legacy_migrated = False

        if filename and source_provider and dest_provider:
            self._db = TrackAssociationDatabase(filename)
        else:
            self._db = None

    def migrate_legacy_entries(self):
        if self._db is None or self._legacy_migrated:
            return
        for source_id, dest_id in self._db.legacy_items(prefix=self._legacy_prefix):
            self._db.upsert(self._source_provider, source_id, self._dest_provider, dest_id)
        self._legacy_migrated = True

    def get(self, track_id: str) -> str | None:
        if track_id in self._mem:
            return self._mem[track_id]
        if self._db is None:
            return None

        dest_id = self._db.get(self._source_provider, track_id, self._dest_provider)
        if dest_id is not None:
            self._mem[track_id] = dest_id
            return dest_id

        legacy_key = f"{self._legacy_prefix}:{track_id}" if self._legacy_prefix else track_id
        legacy_dest_id = self._db.get_legacy_match(legacy_key)
        if legacy_dest_id is not None:
            self.insert(track_id, legacy_dest_id)
            return legacy_dest_id
        return None

    def insert(self, source_id: str, dest_id: str):
        self._mem[source_id] = dest_id
        if self._db is not None:
            self._db.upsert(self._source_provider, source_id, self._dest_provider, dest_id)

    def items(self) -> list[tuple[str, str]]:
        data = {}
        if self._db is not None:
            data.update(dict(self._db.items(self._source_provider, self._dest_provider)))
        data.update(self._mem)
        return list(data.items())

    @property
    def data(self) -> dict[str, str]:
        return dict(self.items())
