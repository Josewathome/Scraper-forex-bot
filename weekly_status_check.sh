#!/bin/bash
# weekly_status_check.sh — runs via host crontab, independent of any Claude Code session.
# Pulls live MT5 account/position status from the scalper-prime container (read-only,
# no live-terminal disruption) and appends a timestamped report to LOGFILE.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGFILE="${HOME}/mt5_weekly_status.log"
CONTAINER="scalper-prime"

{
  echo "############################################################"
  echo "# Weekly check run at $(date -u +"%Y-%m-%d %H:%M:%S UTC")"
  echo "############################################################"
  if ! docker ps --format '{{.Names}}' | grep -qx "${CONTAINER}"; then
    echo "ERROR: container ${CONTAINER} is not running."
  else
    docker cp "${SCRIPT_DIR}/weekly_status_query.py" "${CONTAINER}:/tmp/weekly_status_query.py" 2>&1
    docker exec -u abc -e WINEPREFIX=/config/.wine -e HOME=/config "${CONTAINER}" \
        wine "C:\\Program Files (x86)\\Python39-32\\python.exe" "Z:\\tmp\\weekly_status_query.py" 2>&1
  fi
  echo ""
} >> "${LOGFILE}"
