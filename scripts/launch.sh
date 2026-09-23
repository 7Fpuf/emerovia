#!/usr/bin/env bash
# Launch (or re-launch) the Agent Commons world server persistently.
#
# - Binds 127.0.0.1:8765 ONLY (never public). The --host flag is hardcoded;
#   do not change it without founder approval.
# - Derives AC_OPERATOR_PUBKEY from server/operator.key (public key only in
#   the environment). Without it the operator endpoints answer 503.
# - Idempotent: if the pidfile points at a live server that passes /health,
#   exits 0 without doing anything. Stale pidfiles are cleaned up.
# - Kills only the exact PID recorded in the pidfile (never pkill -f, which
#   risks matching the supervisor's own command line).
#
# Usage: bash scripts/launch.sh
# Exit 0: server healthy. Exit 1: server could not be brought up.

set -euo pipefail
cd "$(dirname "$0")/.."

PIDFILE="server/world.pid"
LOG="server/server.log"

health_ok() {
    curl -s --noproxy '*' -m 5 http://127.0.0.1:8765/health 2>/dev/null \
        | grep -q '"status":"ok"'
}

pid_alive() {
    [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null
}

if [ -f "$PIDFILE" ]; then
    OLD_PID="$(cat "$PIDFILE" 2>/dev/null || true)"
    if pid_alive "$OLD_PID" && health_ok; then
        exit 0
    fi
    # Stale or unhealthy: stop the recorded process if it still exists.
    if pid_alive "$OLD_PID"; then
        kill "$OLD_PID" 2>/dev/null || true
        for _ in $(seq 1 10); do
            pid_alive "$OLD_PID" || break
            sleep 1
        done
        pid_alive "$OLD_PID" && kill -9 "$OLD_PID" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"
elif health_ok; then
    # No pidfile (e.g. launched by an older supervisor) but healthy: adopt it.
    exit 0
fi

if [ ! -x .venv/bin/python ]; then
    echo "launch.sh: .venv/bin/python missing" >&2
    exit 1
fi
if [ ! -f server/operator.key ]; then
    echo "launch.sh: server/operator.key missing" >&2
    exit 1
fi

# Public key only — the private key never leaves operator.key.
export AC_OPERATOR_PUBKEY="$(
    .venv/bin/python -c "
from nacl.signing import SigningKey
raw = open('server/operator.key').read().strip()
print(SigningKey(bytes.fromhex(raw)).verify_key.encode().hex())
"
)"

# Do not let the server inherit the supervisor's lock fd: supervise.sh holds
# flock on server/supervise.lock via fd 9, and flock locks live on the open
# file description, so an inherited fd would keep future supervisors locked
# out forever. Close it before detaching.
exec 9>&- 2>/dev/null || true

# setsid fully detaches from this session's process group so a later
# teardown of the launching session does not take the server with it.
setsid nohup .venv/bin/python -m uvicorn server.app:app \
    --host 127.0.0.1 --port 8765 >> "$LOG" 2>&1 < /dev/null &
echo "$!" > "$PIDFILE"

for _ in $(seq 1 30); do
    if health_ok; then
        exit 0
    fi
    sleep 1
done

echo "launch.sh: server did not pass /health within 30s" >&2
rm -f "$PIDFILE"
exit 1
