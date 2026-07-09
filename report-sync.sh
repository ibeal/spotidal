#!/bin/sh
set -u

started_at=$(date +%s)
if spotidal --autorun; then
    status=up
    message="Sync completed"
    exit_status=0
else
    exit_status=$?
    status=down
    message="Sync failed or completed partially"
fi

finished_at=$(date +%s)
duration_ms=$(( (finished_at - started_at) * 1000 ))

if [ -n "${UPTIME_KUMA_PUSH_URL:-}" ]; then
    if ! curl --fail --silent --show-error --output /dev/null --max-time 10 \
        --get \
        --data-urlencode "status=${status}" \
        --data-urlencode "msg=${message}" \
        --data-urlencode "ping=${duration_ms}" \
        "${UPTIME_KUMA_PUSH_URL}" 2>/dev/null; then
        echo "Unable to report Spotidal sync status to Uptime Kuma." >&2
    fi
fi

exit "${exit_status}"
