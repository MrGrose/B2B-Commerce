set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
IMAGE_REPO="${B2B_COMMERCE_IMAGE_REPO:-b2b-commerce}"
TAG="${B2B_COMMERCE_IMAGE_TAG:-}"
DEPLOY_TAG_FILE="${DEPLOY_TAG_FILE:-$ROOT/.deploy-tag}"
DEPLOY_TAG_PREV_FILE="${DEPLOY_TAG_PREV_FILE:-$ROOT/.deploy-tag.prev}"
HEALTH_TIMEOUT_SEC="${HEALTH_TIMEOUT_SEC:-90}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/api/ready}"
LOGIN_URL="${LOGIN_URL:-http://127.0.0.1:8000/login}"

if [[ -z "$TAG" ]]; then
  echo "ERROR: B2B_COMMERCE_IMAGE_TAG is required" >&2
  exit 1
fi

export B2B_COMMERCE_IMAGE_TAG="$TAG"
export COMPOSE_FILE

dc() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

log() {
  echo "[deploy] $*"
}

require_disk_space() {
  local usage_pct
  usage_pct="$(df -P / | awk 'NR==2 {gsub(/%/,"",$5); print $5}')"
  if [[ -z "$usage_pct" ]]; then
    echo "ERROR: cannot read disk usage" >&2
    exit 1
  fi
  if [[ "$usage_pct" -ge 95 ]]; then
    echo "ERROR: disk usage ${usage_pct}% — free space before deploy" >&2
    exit 1
  fi
  log "disk usage ${usage_pct}%"
}

rollback_image() {
  local prev_tag="${1:-}"
  if [[ -z "$prev_tag" ]]; then
    log "no previous image tag for rollback"
    return 1
  fi
  log "rolling back to image tag ${prev_tag}"
  export B2B_COMMERCE_IMAGE_TAG="$prev_tag"
  dc up -d --no-deps --force-recreate worker api
}

wait_for_health() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT_SEC))
  while (( SECONDS < deadline )); do
    if dc ps --status running api worker 2>/dev/null | grep -q api \
      && dc ps --status running api worker 2>/dev/null | grep -q worker; then
      if curl -fsS "$HEALTH_URL" >/dev/null 2>&1 \
        && curl -fsS "$LOGIN_URL" 2>/dev/null | grep -q 'Вход'; then
        log "health checks passed"
        return 0
      fi
    fi
    sleep 3
  done
  log "health checks failed after ${HEALTH_TIMEOUT_SEC}s"
  return 1
}

MIGRATE_RAN=0
PREV_TAG=""
cleanup_on_fail() {
  local exit_code=$?
  if [[ "$exit_code" -ne 0 ]]; then
    if [[ -n "$PREV_TAG" ]]; then
      rollback_image "$PREV_TAG" || true
    fi
    if [[ "$MIGRATE_RAN" -eq 1 ]]; then
      echo "ERROR: migration may have been applied but deploy failed." >&2
      echo "ERROR: image rolled back; PostgreSQL restore from backup is MANUAL — see docs/DEPLOY.md" >&2
    fi
  fi
}
trap cleanup_on_fail EXIT

log "deploy start tag=${TAG}"

require_disk_space

if [[ -f "$DEPLOY_TAG_FILE" ]]; then
  PREV_TAG="$(tr -d '[:space:]' <"$DEPLOY_TAG_FILE")"
fi
if [[ "${SKIP_GIT_PULL:-}" != "1" ]] && git -C "$ROOT" rev-parse --is-inside-work-tree &>/dev/null; then
  git -C "$ROOT" fetch origin
  git -C "$ROOT" pull --ff-only
  log "git pull --ff-only OK"
fi

chmod +x scripts/backup-postgres.sh 2>/dev/null || true
./scripts/backup-postgres.sh

if [[ "${B2B_COMMERCE_PULL:-1}" == "0" ]]; then
  log "B2B_COMMERCE_PULL=0: building images locally"
  dc build api worker
else
  log "pulling ${IMAGE_REPO}:${TAG}"
  docker pull "${IMAGE_REPO}:${TAG}"
fi

log "stopping worker"
dc stop worker || true

log "running migrations"
dc run --rm --no-deps api alembic upgrade head
MIGRATE_RAN=1
log "starting worker"
dc up -d --no-deps --force-recreate worker

log "recreating api"
dc up -d --no-deps --force-recreate api

if ! wait_for_health; then
  echo "ERROR: deploy health failed" >&2
  exit 1
fi

if [[ -n "$PREV_TAG" && "$PREV_TAG" != "$TAG" ]]; then
  echo "$PREV_TAG" >"$DEPLOY_TAG_PREV_FILE"
fi
echo "$TAG" >"$DEPLOY_TAG_FILE"

trap - EXIT
log "deploy success tag=${TAG}"
