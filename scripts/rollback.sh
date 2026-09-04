set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
DEPLOY_TAG_FILE="${DEPLOY_TAG_FILE:-$ROOT/.deploy-tag}"
DEPLOY_TAG_PREV_FILE="${DEPLOY_TAG_PREV_FILE:-$ROOT/.deploy-tag.prev}"
HEALTH_TIMEOUT_SEC="${HEALTH_TIMEOUT_SEC:-90}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/api/health}"
LOGIN_URL="${LOGIN_URL:-http://127.0.0.1:8000/login}"

if [[ ! -f "$DEPLOY_TAG_PREV_FILE" ]]; then
  echo "ERROR: ${DEPLOY_TAG_PREV_FILE} not found — nothing to roll back to" >&2
  exit 1
fi

PREV_TAG="$(tr -d '[:space:]' <"$DEPLOY_TAG_PREV_FILE")"
if [[ -z "$PREV_TAG" ]]; then
  echo "ERROR: previous tag is empty" >&2
  exit 1
fi

export B2B_COMMERCE_IMAGE_TAG="$PREV_TAG"
export COMPOSE_FILE

dc() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

echo "Rolling back to B2B_COMMERCE_IMAGE_TAG=${PREV_TAG}"
docker pull "b2b-commerce:${PREV_TAG}" || true
dc stop worker || true
dc up -d --no-deps --force-recreate worker api

deadline=$((SECONDS + HEALTH_TIMEOUT_SEC))
while (( SECONDS < deadline )); do
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1 \
    && curl -fsS "$LOGIN_URL" 2>/dev/null | grep -q 'Вход'; then
    if [[ -f "$DEPLOY_TAG_FILE" ]]; then
      cp "$DEPLOY_TAG_FILE" "$DEPLOY_TAG_PREV_FILE"
    fi
    echo "$PREV_TAG" >"$DEPLOY_TAG_FILE"
    echo "Rollback OK tag=${PREV_TAG}"
    exit 0
  fi
  sleep 3
done

echo "ERROR: rollback health failed" >&2
exit 1
