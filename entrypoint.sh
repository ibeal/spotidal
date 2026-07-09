#!/bin/sh
set -eu

echo "${CRON_SCHEDULE} /app/report-sync.sh" > /tmp/spotidal-crontab
exec supercronic /tmp/spotidal-crontab
