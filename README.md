# spotidal

A command line tool for syncing playlists and liked songs between Spotify and Tidal. Supports one-way mirroring and full two-way sync with deletion detection. Optimised for periodic synchronisation of very large collections.

## Prerequisites

This is a [uv](https://docs.astral.sh/uv/) project. Install uv first if you don't have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Installation

Clone this repository:

```bash
git clone https://github.com/Sandruin/spotidal.git && cd spotidal
```

Dependencies are installed automatically on first `uv run`.

## Quick start

Run without arguments to enter the interactive setup wizard:

```bash
uv run spotidal
```

The wizard walks you through:

1. **Spotify credentials** -- create an app at [developer.spotify.com](https://developer.spotify.com/dashboard), enter your client ID, secret, username, and redirect URI
2. **Tidal login** -- opens your browser for OAuth authentication
3. **Sync mode** -- two-way (keep both services in sync) or one-way (mirror from one to the other)
4. **Playlist selection** -- checkbox list of all your playlists from both services
5. **Favorites** -- whether to sync liked/saved songs
6. **Deletion behavior** -- (two-way only) whether removing a track on one side should remove it from the other

Your choices are saved to `config.yml`. At the end you can run the sync immediately or save and exit.

## Usage

```
spotidal                # interactive setup wizard
spotidal --setup        # same as above
spotidal --autorun      # run sync using saved config (non-interactive, cron-friendly)
spotidal --oneshot      # interactive one-shot sync (pick playlists without saving to config)
spotidal --import-manual-matches  # import completed songs_not_found.yml entries without API calls
spotidal --rebuild-snapshots      # rebuild two-way snapshots without changing playlists
spotidal --config FILE  # use a different config file (works with any mode)
```

## Sync modes

### One-way

- Mirrors the source playlist to the destination, including track ordering
- Tracks only on the destination side are left untouched
- Tracks removed from the source are **not** removed from the destination
- If the track order differs, the destination playlist is cleared and rewritten
- Set `sync.allow_deletions: true` to always clear and rewrite destination playlists so they exactly match the source

### Two-way

- Tracks added to either side are copied to the other
- Track ordering is **not** synced; new tracks are appended to the end
- Deletions are detected via a local snapshot stored in `.cache.db`:
  - On the first run, a snapshot of all matched tracks is saved; no deletions are detected
  - On subsequent runs, if a track was in the previous snapshot but is now missing from one side, it is treated as a deletion and removed from the other side (if `allow_deletions` is enabled)
- If a track cannot be found on the other platform, it is appended to `songs_not_found.yml`. Fill in the missing `tidal_id` or `spotify_id` using the track's share URL, then run `spotidal --import-manual-matches`. This writes the pair to `.cache.db` without searching either API and removes the completed queue entry.
- On first use, an existing `songs_not_found.txt` is migrated to YAML and retained unchanged.

`allow_deletions` has different effects by mode:
- In `two-way`, it propagates track removals across services
- In `one-way`, it makes destination playlists exact mirrors of the source by clearing and rewriting them when needed

After restoring playlists or correcting a two-way baseline, run `spotidal --rebuild-snapshots` before enabling two-way deletions. It reads the configured Spotify/Tidal playlist pairs and replaces their local snapshots from tracks matched on both sides; it does not search, add, remove, or clear provider data.


> **Note:** If you previously used this tool with read-only Spotify permissions, delete the `.cache` file in the project root and re-authenticate to grant write permissions needed for reverse or bidirectional sync.

## Docker

Prebuilt images are published to `ghcr.io/ibeal/spotidal` on every push to `main` and on version tags.

The container runs `--autorun` on a schedule (via [supercronic](https://github.com/aptible/supercronic)) rather than once, so it's meant to be left running:

```bash
docker run -d --restart unless-stopped \
  -v "$(pwd)/data:/data" \
  -e CRON_SCHEDULE="0 */6 * * *" \
  ghcr.io/ibeal/spotidal
```

or with `docker compose up -d` using the provided `docker-compose.yml`. `CRON_SCHEDULE` takes standard 5-field cron syntax and defaults to every 6 hours. `/data` should contain `config.yml` (and, after the first run, `.session.yml` and `.cache.db`).

### Uptime Kuma

To monitor scheduled syncs, create an Uptime Kuma **Push** monitor and set its generated URL on the host (for example, in the Compose `.env` file):

```env
UPTIME_KUMA_PUSH_URL=https://kuma.example.com/api/push/your-monitor-token
```

Spotidal reports each completed cron run to that URL. A fully successful sync reports `up`; a failed or partial sync reports `down` and leaves the job non-zero in the container logs. Configure the Push monitor's heartbeat interval to the cron schedule plus the longest expected sync duration. If no run completes in that window, Kuma alerts. The URL is optional: deployments without it retain their existing behavior.

If Spotify responds with a very large `Retry-After`, set `max_wait_for_rate_limit` in `config.yml` to cap how long a run is allowed to wait. The default is `3600` seconds. If Spotify asks for more than that, the current run exits cleanly and keeps any work already completed, which is safer for cron-style deployments like supercronic.
The interactive `--setup` wizard needs a TTY and opens a browser for Tidal OAuth, so it's best run with `uv run spotidal` on the host rather than in a container. To run a one-off command (`--setup`, `--oneshot`) in the container instead of starting the cron loop, override the entrypoint:

```bash
docker run --rm -it --entrypoint spotidal -v "$(pwd)/data:/data" ghcr.io/ibeal/spotidal --setup
```

## Acknowledgements

This project is a fork of [spotify2tidal/spotify_to_tidal](https://github.com/spotify2tidal/spotify_to_tidal). Thanks to the original authors and contributors for building the foundation this project is built on.

## AI disclaimer

Claude Code was used to accelerate development. All code was reviewed and approved manually.
