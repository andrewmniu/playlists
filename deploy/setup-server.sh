#!/usr/bin/env bash
#
# One-time server bootstrap for the playlists VPS (Hetzner, Ubuntu 24.04).
# Run as root on the fresh box:  ssh root@<SERVER_IP> 'bash -s' < deploy/setup-server.sh
#
# It is safe to re-run (idempotent where practical).
#
set -euo pipefail

DEPLOY_USER="andrew"
DB_NAME="listening_history"
DB_ROLE="andrew"

echo "==> Updating packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y

echo "==> Installing Postgres + basics"
# postgresql-16 is the default on Ubuntu 24.04. pgvector (postgresql-16-pgvector)
# is intentionally NOT installed yet — that's a phase-2 add when we build embeddings.
apt-get install -y postgresql postgresql-contrib ufw git python3-venv python3-pip

echo "==> Creating deploy user '${DEPLOY_USER}' (sudo, key-only)"
if ! id -u "${DEPLOY_USER}" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "" "${DEPLOY_USER}"
  usermod -aG sudo "${DEPLOY_USER}"
  # passwordless sudo so cron/automation never blocks on a prompt
  echo "${DEPLOY_USER} ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-${DEPLOY_USER}"
  chmod 440 "/etc/sudoers.d/90-${DEPLOY_USER}"
fi
# copy root's authorized_keys (the key you added in the Hetzner console) to the new user
install -d -m 700 -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" "/home/${DEPLOY_USER}/.ssh"
cp /root/.ssh/authorized_keys "/home/${DEPLOY_USER}/.ssh/authorized_keys"
chown "${DEPLOY_USER}:${DEPLOY_USER}" "/home/${DEPLOY_USER}/.ssh/authorized_keys"
chmod 600 "/home/${DEPLOY_USER}/.ssh/authorized_keys"

echo "==> Firewall: SSH only (Postgres stays bound to localhost, never exposed)"
ufw allow OpenSSH
ufw --force enable

echo "==> Creating Postgres role + database"
# Generate a strong DB password once; reuse if the role already exists.
DB_PASSWORD="$(openssl rand -base64 24 | tr -d '/+=' | head -c 28)"
if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${DB_ROLE}'" | grep -q 1; then
  echo "    role ${DB_ROLE} already exists — leaving its password unchanged"
  DB_PASSWORD="(unchanged — see the password you saved on first run)"
else
  sudo -u postgres psql -c "CREATE ROLE ${DB_ROLE} LOGIN PASSWORD '${DB_PASSWORD}';"
fi
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
  sudo -u postgres createdb -O "${DB_ROLE}" "${DB_NAME}"
fi

echo "==> Hardening SSH (disable root login + password auth)"
SSHD=/etc/ssh/sshd_config.d/99-hardening.conf
cat > "${SSHD}" <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF
systemctl reload ssh || systemctl reload sshd

cat <<EOF

============================================================
 SERVER READY
============================================================
 Deploy user : ${DEPLOY_USER}   (key-only, sudo)
 Database    : ${DB_NAME}
 DB role     : ${DB_ROLE}
 DB password : ${DB_PASSWORD}
 Connect (local on box): psql -U ${DB_ROLE} -h localhost ${DB_NAME}
============================================================

 IMPORTANT: root login is now disabled. Before closing this
 session, open a NEW terminal and confirm you can log in as:
     ssh ${DEPLOY_USER}@<SERVER_IP>
 If that works, you're safe to disconnect root.

 Save the DB password above — you'll need it for the migration
 and the playlist scripts' connection string.
============================================================
EOF
