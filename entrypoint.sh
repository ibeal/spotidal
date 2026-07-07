#!/bin/sh
set -eu

echo "${CRON_SCHEDULE} spotidal --autorun" > /tmp/spotidal-crontab
exec supercronic /tmp/spotidal-crontab
