import sys
import argparse

from spotidal.config import load_config
from spotidal.errors import AuthenticationError, SyncAbortError


def main():
    parser = argparse.ArgumentParser(
        description="Sync playlists and favorites between Spotify and Tidal",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--autorun", action="store_true", help="run sync using saved configuration")
    group.add_argument("--setup", action="store_true", help="enter interactive setup wizard")
    group.add_argument("--oneshot", action="store_true", help="interactive one-shot sync (doesn't save sync selections)")
    group.add_argument(
        "--import-manual-matches",
        action="store_true",
        help="import completed songs_not_found.yml matches without contacting providers",
    )
    group.add_argument(
        "--rebuild-snapshots",
        action="store_true",
        help="rebuild two-way playlist snapshots without searching or changing provider data",
    )
    parser.add_argument("--config", default="config.yml", help="path to config file (default: config.yml)")
    args = parser.parse_args()

    config_path = args.config
    if args.import_manual_matches:
        from spotidal.manual_matches import import_manual_matches
        from spotidal.run import _data_path

        try:
            imported, remaining, migrated = import_manual_matches(
                _data_path(config_path, "songs_not_found.yml"),
                _data_path(config_path, ".cache.db"),
                legacy_queue_path="songs_not_found.txt",
            )
        except ValueError as e:
            parser.error(str(e))
        if migrated:
            print("Migrated songs_not_found.txt to songs_not_found.yml; the text file was retained.")
        print(f"Imported {imported} manual match(es); {remaining} queue entry(s) remain.")
        return

    config = load_config(config_path)

    try:
        if args.autorun:
            from spotidal.run import run_sync

            if config is None:
                print(f"No config found at '{config_path}'. Run `spotidal` first to set up.")
                sys.exit(1)
            run_sync(config, config_path)
        elif args.rebuild_snapshots:
            from spotidal.run import run_rebuild_snapshots

            if config is None:
                print(f"No config found at '{config_path}'.")
                sys.exit(1)
            run_rebuild_snapshots(config, config_path)
        elif args.oneshot:
            from spotidal.run import run_oneshot

            run_oneshot(config, config_path)
        else:
            from spotidal.setup import run_wizard
            from spotidal.run import run_sync

            config, action = run_wizard(config, config_path)
            if action == "save_and_run":
                run_sync(config, config_path)
            elif action == "cancel":
                print("Setup cancelled.")
    except AuthenticationError as e:
        print(f"Authentication error: {e}")
        sys.exit(1)
    except SyncAbortError as e:
        print(f"Sync aborted: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
