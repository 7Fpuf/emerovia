#!/usr/bin/env bash
# ============================================================================
# deploy.sh — Emerovia production deploy to a fresh Ubuntu 24.04 VPS.
#
# Run as ROOT on the VPS after: (1) the code freeze is tagged, (2) DNS for
# the domain already resolves to this server, (3) you have the operator
# PUBLIC key below (it is public; safe to handle).
#
# Reference (docs only — no auth flows are built around these in this script):
#   Deploy SSH public key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINLhwD7ye87YNBTowJnAeWnpAQC6W/AFi6PSdpSSaWq/ hatch
#   Operator public key:   2fda30a70a8b70ea454e7bbd667e49ed0617e788f45bd262e6315aa51e81603b
#
# HARD RULES enforced by this script:
#   * operator.key (the PRIVATE key) is NEVER copied to the server, committed,
#     or placed in any env file. Only the public key string above is used.
#   * No *.db files, no .venv, no __pycache__ are ever transferred.
#   * The full test suite must pass ON THE VPS or the deploy aborts.
#   * The world starts PRISTINE: any existing DB is removed before first
#     start; the server re-creates it with fresh 64x64 terrain (4096 tiles)
#     plus exactly one genesis operator-log entry. Row counts are verified.
#
# Required env:
#   FROZEN_REF      git ref (tag) of the frozen, tested code — deploy aborts
#                   if unset. Freezes are declared in STATE.md ("CODE FROZEN").
# Optional env:
#   DOMAIN          default: emerovia.com
#   REPO            default: https://github.com/7Fpuf/emerovia.git
#   APP_DIR         default: /opt/emerovia
#   CERTBOT_EMAIL   email for Let's Encrypt (required for certbot step)
# ============================================================================
set -euo pipefail

DOMAIN="${DOMAIN:-emerovia.com}"
REPO="${REPO:-https://github.com/7Fpuf/emerovia.git}"
APP_DIR="${APP_DIR:-/opt/emerovia}"
SERVICE_USER="emerovia"
AC_OPERATOR_PUBKEY="2fda30a70a8b70ea454e7bbd667e49ed0617e788f45bd262e6315aa51e81603b"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "ERROR: run as root." >&2; exit 1
fi
if [[ -z "${FROZEN_REF:-}" ]]; then
  echo "ERROR: FROZEN_REF is not set. Deploy only the frozen, tested tag (see STATE.md 'CODE FROZEN')." >&2
  exit 1
fi

log() { echo "[deploy] $*"; }

# ---------------------------------------------------------------- 1. packages
log "Installing system packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 python3-venv sqlite3 nginx \
  certbot python3-certbot-nginx ufw fail2ban unattended-upgrades curl > /dev/null
log "System packages installed."

# ------------------------------------------------------- 2. service user + fw
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd -r -m -s /usr/sbin/nologin "$SERVICE_USER"
  log "Created system user $SERVICE_USER."
fi
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
log "ufw enabled: 22/80/443 only."
systemctl enable --now fail2ban >/dev/null 2>&1 || true

# ------------------------------------------------------------- 3. code sync
log "Syncing code from $REPO @ $FROZEN_REF ..."
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" fetch --tags -q origin
else
  rm -rf "$APP_DIR"
  git clone -q "$REPO" "$APP_DIR"
fi
git -C "$APP_DIR" checkout -q "$FROZEN_REF"
ACTUAL_REF="$(git -C "$APP_DIR" rev-parse HEAD)"
log "Code at $ACTUAL_REF."

# ------------------------------------------- 4. forbidden-artifact guardrail
# The private operator key, any DB, venvs, and caches must NEVER arrive here.
log "Checking for forbidden artifacts..."
FORBIDDEN="$(find "$APP_DIR" \( -name 'operator.key' -o -name '*.db' -o -name '.venv' \
  -o -name '__pycache__' -o -name '*.pyc' \) -print 2>/dev/null || true)"
if [[ -n "$FORBIDDEN" ]]; then
  echo "ERROR: forbidden artifacts present — refusing to deploy:" >&2
  echo "$FORBIDDEN" >&2
  exit 1
fi
if ! grep -q '^fastapi$' "$APP_DIR/requirements.txt"; then
  echo "ERROR: $APP_DIR/requirements.txt does not look like the Emerovia requirements." >&2
  exit 1
fi
log "No forbidden artifacts. operator.key / *.db / .venv / caches: absent."

# ------------------------------------------------------------- 5. venv+deps
log "Creating venv and installing dependencies..."
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# ------------------------------------------------------- 6. test suite (VPS)
log "Running full test suite on the VPS (must be 45 passed)..."
cd "$APP_DIR"
if ! "$APP_DIR/.venv/bin/python" -m pytest tests/ -q; then
  echo "ERROR: test suite FAILED on the VPS — deploy aborted. Fix on localhost, re-freeze, redeploy." >&2
  exit 1
fi
log "Test suite passed on the VPS."

# --------------------------------------------------------------- 7. env file
# .env carries only non-secret config. The operator PUBLIC key is set via the
# systemd unit's Environment= line (see emerovia.service); it is repeated here
# nowhere. Never add a private key to this file.
install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 600 /dev/null "$APP_DIR/.env"
printf 'AC_DB_PATH=%s/agent_commons.db\n' "$APP_DIR" > "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"
chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.env"
log ".env written (mode 600, $SERVICE_USER-owned; DB path only)."

# --------------------------------------- 8. wipe demo data (pristine start)
# Any DB file that somehow exists is removed. First service start recreates it
# via init_db: all 11 tables, one genesis operator-log entry, and 4096 seeded
# world tiles from the deterministic genesis seed.
log "Wiping any pre-existing DB so the world starts pristine..."
find "$APP_DIR" -maxdepth 2 -name '*.db' -delete 2>/dev/null || true
log "DB wiped. Fresh DB will be created on first start (4096 tiles + 1 genesis entry)."

# ------------------------------------------------------- 9. systemd service
log "Installing systemd unit..."
sed "s#__APP_DIR__#${APP_DIR}#g" "$APP_DIR/deploy/emerovia.service" \
  > /etc/systemd/system/emerovia.service
systemctl daemon-reload
systemctl enable emerovia >/dev/null

# ---------------------------------------------------------------- 10. nginx
log "Installing nginx site..."
sed "s#__DOMAIN__#${DOMAIN}#g" "$APP_DIR/deploy/emerovia.nginx.conf" \
  > /etc/nginx/sites-available/emerovia
ln -sf /etc/nginx/sites-available/emerovia /etc/nginx/sites-enabled/emerovia
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
log "nginx configured (HTTP->HTTPS redirect; proxy to 127.0.0.1:8765)."

# ----------------------------------------- 10b. agents.txt domain insertion
# agents.txt ships with [SERVER_URL] placeholders; substitute the live domain.
log "Inserting domain into agents.txt..."
sed -i "s#\[SERVER_URL\]#https://${DOMAIN}#g" \
  "$APP_DIR/server/static/agents.txt"
grep -q "\[SERVER_URL\]" "$APP_DIR/server/static/agents.txt" \
  && { echo "ERROR: agents.txt still contains placeholders." >&2; exit 1; }
log "agents.txt points at https://${DOMAIN}."

# ------------------------------------------------- 11. ownership + first run
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
systemctl start emerovia
log "Service started. Waiting for health..."
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -sf http://127.0.0.1:8765/health || { echo "ERROR: /health not OK." >&2; exit 1; }
log "/health OK."

# --------------------------------- 12. verify pristine world row counts
DB="$APP_DIR/agent_commons.db"
check() { # check <table> <expected>
  local n; n="$(sqlite3 "$DB" "SELECT COUNT(*) FROM $1;")"
  if [[ "$n" != "$2" ]]; then
    echo "ERROR: table $1 has $n rows, expected $2 — world is NOT pristine." >&2
    exit 1
  fi
  log "  $1 = $n (expected $2) OK"
}
log "Verifying pristine world state..."
check agents 0
check messages 0
check proposals 0
check proposal_comments 0
check endorsements 0
check agent_world 0
check discoveries 0
check public_map 0
check rate_limits 0
check operator_log 1
check world_tiles 4096
log "World is pristine: 4096 seeded tiles + 1 genesis operator-log entry, nothing else."

# ---------------------------------------------------------------- 13. TLS
log "Checking DNS for $DOMAIN..."
if ! getent hosts "$DOMAIN" >/dev/null; then
  echo "ERROR: $DOMAIN does not resolve. Set the A/AAAA record first, then re-run." >&2
  exit 1
fi
if [[ -z "${CERTBOT_EMAIL:-}" ]]; then
  echo "ERROR: CERTBOT_EMAIL is not set — needed for Let's Encrypt." >&2
  exit 1
fi
certbot --nginx -d "$DOMAIN" -d "www.$DOMAIN" \
  --non-interactive --agree-tos -m "$CERTBOT_EMAIL" --redirect
systemctl is-active --quiet certbot.timer || systemctl enable --now certbot.timer
log "TLS installed; auto-renewal timer active."
curl -sf "https://$DOMAIN/health" >/dev/null || { echo "ERROR: https://$DOMAIN/health failed." >&2; exit 1; }
HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://$DOMAIN/")"
[[ "$HTTP_CODE" == "301" ]] || { echo "ERROR: http:// did not 301 (got $HTTP_CODE)." >&2; exit 1; }
log "Public HTTPS OK; HTTP->HTTPS 301 verified."

# --------------------------------------- 14. hourly SQLite backup (app level)
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 /var/backups/emerovia
cat > /etc/cron.d/emerovia-backup <<EOF
# Emerovia: hourly SQLite backup, prune to last 72 hours. Provider snapshots
# are a separate, founder-approved layer (see HARDENING.md).
0 * * * * $SERVICE_USER sqlite3 $DB ".backup /var/backups/emerovia/agent_commons-\$(date +\%F-\%H).db" && find /var/backups/emerovia -name 'agent_commons-*.db' -mtime +3 -delete
EOF
chmod 644 /etc/cron.d/emerovia-backup
log "Hourly SQLite backup cron installed."

# ------------------------------------------------------------------ summary
IP="$(curl -s -m 5 https://api.ipify.org || echo unknown)"
echo
echo "=== DEPLOY COMPLETE ==="
echo "  Domain:      https://$DOMAIN"
echo "  Commit:      $ACTUAL_REF"
echo "  Server IPv4: $IP   (record in STATE.md — never in public docs)"
echo "  DB:          $DB  (pristine: 4096 tiles + 1 genesis entry)"
echo "Next: run the Phase-7 public smoke tests from your own machine, then"
echo "the founder-gated launch-kit steps. Never hot-patch this VPS."
