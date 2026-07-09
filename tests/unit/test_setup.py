from spotidal.setup import _build_playlist_choices
from spotidal.type.models import Playlist


def test_build_playlist_choices_marks_existing_playlists_enabled():
    spotify_playlists = [
        Playlist(provider_id="sp-1", name="Keep", description=""),
        Playlist(provider_id="sp-2", name="Skip", description=""),
    ]
    tidal_playlists = [
        Playlist(provider_id="td-1", name="Keep", description=""),
        Playlist(provider_id="td-2", name="Other", description=""),
    ]

    choices = _build_playlist_choices(
        spotify_playlists=spotify_playlists,
        tidal_playlists=tidal_playlists,
        mode="one-way",
        direction="tidal-to-spotify",
        existing_playlists=[{"name": "Keep", "spotify_id": "sp-1", "tidal_id": "td-1"}],
    )

    keep_choice = next(choice for choice in choices if choice["value"] == "sp-1|td-1|Keep")
    other_choice = next(choice for choice in choices if choice["value"] == "|td-2|Other")

    assert keep_choice["enabled"] is True
    assert other_choice["enabled"] is False

