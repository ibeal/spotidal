import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "report-sync.sh"


def _make_command(path: Path, body: str):
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def _run_report_sync(tmp_path: Path, sync_exit_status: int, push_url: str | None = "https://kuma.test/push/token"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl_args = tmp_path / "curl-args"
    _make_command(bin_dir / "spotidal", f"exit {sync_exit_status}")
    _make_command(bin_dir / "curl", 'printf "%s\\n" "$@" > "$CURL_ARGS_PATH"')
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "CURL_ARGS_PATH": str(curl_args),
    }
    if push_url is not None:
        environment["UPTIME_KUMA_PUSH_URL"] = push_url

    result = subprocess.run(["/bin/sh", SCRIPT], env=environment, capture_output=True, text=True)
    return result, curl_args


def test_report_sync_reports_success_to_uptime_kuma(tmp_path):
    result, curl_args = _run_report_sync(tmp_path, sync_exit_status=0)

    assert result.returncode == 0
    assert "status=up" in curl_args.read_text()
    assert "msg=Sync completed" in curl_args.read_text()


def test_report_sync_reports_failure_and_preserves_exit_status(tmp_path):
    result, curl_args = _run_report_sync(tmp_path, sync_exit_status=7)

    assert result.returncode == 7
    assert "status=down" in curl_args.read_text()
    assert "msg=Sync failed or completed partially" in curl_args.read_text()


def test_report_sync_skips_uptime_kuma_when_url_is_unset(tmp_path):
    result, curl_args = _run_report_sync(tmp_path, sync_exit_status=0, push_url=None)

    assert result.returncode == 0
    assert not curl_args.exists()
