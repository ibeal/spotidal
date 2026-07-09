import sys

import pytest

from spotidal.__main__ import main


def test_autorun_exits_nonzero_after_partial_sync(mocker, monkeypatch):
    mocker.patch("spotidal.__main__.load_config", return_value={})
    run_sync = mocker.patch("spotidal.run.run_sync", return_value=False)
    monkeypatch.setattr(sys, "argv", ["spotidal", "--autorun"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1
    run_sync.assert_called_once_with({}, "config.yml")


def test_autorun_returns_normally_after_complete_sync(mocker, monkeypatch):
    mocker.patch("spotidal.__main__.load_config", return_value={})
    run_sync = mocker.patch("spotidal.run.run_sync", return_value=True)
    monkeypatch.setattr(sys, "argv", ["spotidal", "--autorun"])

    main()

    run_sync.assert_called_once_with({}, "config.yml")
