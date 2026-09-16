#!/bin/bash
# Pi thermal/throttle/memory snapshot — single-shot or looped, with a log file.
#
# Usage:
#   ./monitor.sh          single snapshot, printed to stdout + appended to a log file
#   ./monitor.sh 30       loop every 30s until Ctrl+C, same output each time
#
# Each run creates its own timestamped log file (monitor_log_YYYYMMDD_HHMMSS.txt)
# so separate monitoring sessions don't overwrite each other — useful for
# lining up a specific benchmark run's timeline against temp/throttle/swap
# after the fact.

INTERVAL="$1"
LOGFILE="monitor_log_$(date +%Y%m%d_%H%M%S).txt"

snapshot() {
  local ts
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  {
    echo "=== $ts ==="
    vcgencmd measure_temp
    vcgencmd get_throttled
    free -h
    echo ""
  } | tee -a "$LOGFILE"
}

if [ -z "$INTERVAL" ]; then
  snapshot
else
  echo "Logging every ${INTERVAL}s to $LOGFILE — Ctrl+C to stop."
  while true; do
    snapshot
    sleep "$INTERVAL"
  done
fi
