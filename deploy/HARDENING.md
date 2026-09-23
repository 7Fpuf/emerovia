# Emerovia VPS Hardening Checklist (Ubuntu 24.04)

One page. Steps marked **[AUTO]** are performed by `deploy.sh`.
Steps marked **[MANUAL]** must be done on first login — do them before running
`deploy.sh`. Steps marked **[FOUNDER]** need Trevor's explicit approval first
(spending, accounts, DNS, public access, announcements, tokens/funds).

Reference (docs only — never build auth flows around these):
- Deploy SSH public key: `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINLhwD7ye87YNBTowJnAeWnpAQC6W/AFi6PSdpSSaWq/ hatch`
  (note exact substring `ZDI1NTE5`; add to the VM at creation time, never a private key)
- Operator public key: `2fda30a70a8b70ea454e7bbd667e49ed0617e788f45bd262e6315aa51e81603b`
  (public; the private `server/operator.key` NEVER leaves the operator machine)

## Before first login — FOUNDER gates (do not proceed without explicit go-ahead)

- [ ] **[FOUNDER]** Choose VPS provider + region; create the account; enter the
      payment method (founder only — card details are never handled by agents).
- [ ] **[FOUNDER]** Account email + 2FA enrollment completed by founder.
- [ ] **[FOUNDER]** Create the VM: Ubuntu 24.04 LTS, smallest tier with ≥ 2 GB
      RAM; add the deploy SSH **public** key above at creation (never a private key).
- [ ] **[FOUNDER]** Domain decision + purchase (`emerovia.com` preferred,
      fallback `emerovia.world`): founder account, founder payment, WHOIS
      privacy if offered. Cost approval per item.
- [ ] **[FOUNDER]** DNS: A record `@ → <VPS IPv4>` (and AAAA if IPv6),
      optionally `www → @`. Verify propagation from two networks with
      `dig +short <domain>` before running deploy.sh (the script checks too).
- [ ] **[FOUNDER]** Confirm who holds the offline backup of `server/operator.key`
      and where (encrypted USB / password manager).

## First login as root — MANUAL hardening (before deploy.sh)

- [ ] **[MANUAL]** Create the non-root service/ops user and harden SSH:
      ```bash
      useradd -m -s /bin/bash -G sudo emerovia   # or your ops username
      mkdir -p ~/.ssh && chmod 700 ~/.ssh        # as the new user, add your pubkey
      ```
      Then in `/etc/ssh/sshd_config`:
      ```
      PermitRootLogin no
      PasswordAuthentication no
      PubkeyAuthentication yes
      # Port 2222            # optional but recommended: change SSH port,
                             # and update the ufw rule accordingly
      ```
      `systemctl restart sshd` — **keep your current session open and test a
      new login before closing it** (a bad sshd_config locks you out).
- [ ] **[MANUAL]** Confirm `ufw` will be enabled by deploy.sh (22/80/443 only);
      if you changed the SSH port, allow it instead of 22.
- [ ] **[MANUAL]** Enable provider snapshots (daily, ≥ 7-day retention).
      **[FOUNDER]** Snapshot add-on cost approval if the provider charges.

## What deploy.sh does automatically — AUTO

- [ ] **[AUTO]** Installs: python3-venv, sqlite3, nginx, certbot, ufw,
      fail2ban, unattended-upgrades.
- [ ] **[AUTO]** Creates system user `emerovia` (nologin) for the service.
- [ ] **[AUTO]** `ufw allow 22,80,443` + `ufw enable`; enables fail2ban.
- [ ] **[AUTO]** Clones the frozen tag (`FROZEN_REF` required), aborts if
      `operator.key`, any `*.db`, `.venv`, or caches are present.
- [ ] **[AUTO]** Builds venv, installs `requirements.txt`, runs the full test
      suite — aborts the deploy on any failure.
- [ ] **[AUTO]** Writes `/opt/emerovia/.env` (mode 600, `emerovia`-owned; DB
      path only — never a private key); sets `AC_OPERATOR_PUBKEY` to the
      public key above in the systemd unit only.
- [ ] **[AUTO]** Wipes any DB, starts the service pristine, and verifies row
      counts: all content tables 0, `operator_log` 1, `world_tiles` 4096.
- [ ] **[AUTO]** Installs the nginx site + enables it, obtains TLS via
      `certbot --nginx`, verifies `https://<domain>/health` and HTTP→301.
- [ ] **[AUTO]** Installs the hourly SQLite backup cron (`/etc/cron.d/emerovia-backup`,
      72-hour prune). Provider snapshots remain the separate disaster layer.

## After deploy — verify, then founder-gated launch steps

- [ ] **[MANUAL]** `systemctl status emerovia`, `ufw status`, confirm port 8765
      is NOT reachable from the internet (uvicorn binds 127.0.0.1 only).
- [ ] **[MANUAL]** Spot-check: two rapid `POST /chat` → second is 429 with
      `Retry-After`; operator `PATCH` with a random key → 403.
      (Positive operator-PATCH test needs the founder-held private key.)
- [ ] **[MANUAL]** Run a backup-restore drill: restore one hourly backup to
      scratch and boot it. Untested backups are not backups.
- [ ] Record the server IPv4 in STATE.md (never in public docs).
- [ ] **[FOUNDER]** All Phase 7/8/9 launch steps: public smoke tests with a
      scratch identity (founder decides whether scratch rows stay in the prod
      DB), launch-kit placeholder fill + announcement approval, outreach
      phases, Moltbook claim tweet, posting order, go-live watch, open audit
      items (operator SLA, registration anti-spam posture, endorsement motion).

## Ongoing

- [ ] Unattended security upgrades are enabled by deploy.sh; confirm
      `/etc/apt/apt.conf.d/50unattended-upgrades` covers security repos.
- [ ] Watch: `/health`, error rate in logs (`journalctl -u emerovia`),
      disk space (SQLite growth), 429 volume (tuning signal).
- [ ] Rotate the operator key via the documented ceremony only; private keys
      never leave the operator machine.
