set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/b2b-commerce}"
GIT_BRANCH="${GIT_BRANCH:-main}"
GIT_REPO="${GIT_REPO:-}"
if [[ -z "$GIT_REPO" ]]; then
  echo "ERROR: set GIT_REPO to your public clone URL (no default private remote)" >&2
  exit 1
fi
DEPLOY_USER="${DEPLOY_USER:-deploy}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/b2b_commerce}"

export DEBIAN_FRONTEND=noninteractive

echo "== 1/6: packages =="
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 git curl ca-certificates ufw openssl
systemctl enable --now docker

echo "== 2/6: deploy user =="
if ! id "$DEPLOY_USER" &>/dev/null; then
  useradd -m -s /bin/bash "$DEPLOY_USER"
fi
usermod -aG docker "$DEPLOY_USER"

echo "== 3/6: repository =="
mkdir -p "$(dirname "$REPO_DIR")"
if [[ ! -d "$REPO_DIR/.git" ]]; then
  git clone "$GIT_REPO" "$REPO_DIR"
fi
cd "$REPO_DIR"
git fetch origin
git checkout "$GIT_BRANCH"
git pull --ff-only origin "$GIT_BRANCH" || git reset --hard "origin/$GIT_BRANCH"
chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "$REPO_DIR"

echo "== 4/6: .env =="
if [[ ! -f "$REPO_DIR/.env" ]]; then
  cp "$REPO_DIR/.env.prod.example" "$REPO_DIR/.env"
  chmod 600 "$REPO_DIR/.env"
  chown "${DEPLOY_USER}:${DEPLOY_USER}" "$REPO_DIR/.env"
  ADMIN_PASSWORD="$(openssl rand -base64 24 | tr -dc 'A-Za-z0-9' | head -c 24)"
  echo "CREATED_ENV=1"
  echo "ADMIN_PASSWORD=${ADMIN_PASSWORD}" >"/root/b2b-commerce-admin-password.txt"
  chmod 600 "/root/b2b-commerce-admin-password.txt"
  echo "Edit ${REPO_DIR}/.env (replace CHANGE_ME). Admin password: /root/b2b-commerce-admin-password.txt"
else
  echo "CREATED_ENV=0 (.env already exists)"
fi

echo "== 5/6: backups and firewall =="
mkdir -p "$BACKUP_DIR"
chown "${DEPLOY_USER}:${DEPLOY_USER}" "$BACKUP_DIR"
ufw --force reset || true
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

chmod +x "$REPO_DIR"/scripts/*.sh 2>/dev/null || true

echo "== 6/6: GHCR pull =="
echo "Run as ${DEPLOY_USER}:"
echo "  docker login ghcr.io -u <github-user> -p <PAT read:packages>"
echo ""
echo "Bootstrap done. Next:"
echo "  1) Edit ${REPO_DIR}/.env (DOMAIN, ALLOWED_HOSTS, supplier fields)"
echo "  2) B2B_COMMERCE_IMAGE_TAG=<sha> ${REPO_DIR}/scripts/deploy.sh"
echo "  3) ${REPO_DIR}/scripts/backup-postgres.sh via cron (02:55 daily)"
