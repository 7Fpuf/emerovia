#!/usr/bin/env bash
# Supervise the Agent Commons world server for up to DURATION seconds
# (default 780, just under the 15-minute heartbeat interval).
#
# Every 60s: check /health; if it fails, run scripts/launch.sh.
# State transitions are appended to server/heartbeat.log; steady "ok"
# checks stay quiet to keep the log small.
#
# Usage: bash scripts/supervise.sh [duration_seconds]
# Exit 0: server healthy at the end. Exit 1: server down at the end
#         (the heartbeat agent uses this for its alert rule).

set -u
cd "$(dirname "$0")/.."

DURATION="${1:-780}"
INTERVAL=60
LOG="server/heartbeat.log"
LOCK="server/supervise.lock"

# Serialize: at most one supervisor at a time (overlapping cron runs exit).
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "$(date -u +%FT%TZ) supervise: another supervisor holds the lock, exiting" >> "$LOG"
    exit 0
fi

health_ok() {
    curl -s --noproxy '*' -m 5 http://127.0.0.1:8765/health 2>/dev/null \
        | grep -q '"status":"ok"'
}

log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }

failures=0
was_down=false
end=$(( $(date +%s) + DURATION ))

while [ "$(date +%s)" -lt "$end" ]; do
    if health_ok; then
        if [ "$was_down" = true ]; then
            log "supervise: server recovered, health ok"
            was_down=false
        fi
    else
        if [ "$was_down" = false ]; then
            log "supervise: health check failed, launching server"
        fi
        if bash scripts/launch.sh >/dev/null 2>&1; then
            log "supervise: launch ok, health now ok"
            was_down=false
        else
            failures=$((failures + 1))
            log "supervise: launch FAILED (consecutive failures: $failures)"
            was_down=true
        fi
    fi
    sleep "$INTERVAL"
done

if health_ok; then
    exit 0
fi
log "supervise: ended with server DOWN"
exit 1
