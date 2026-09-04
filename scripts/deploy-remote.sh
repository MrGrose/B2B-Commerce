set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_ENV="${ROOT}/scripts/deploy.env"
if [[ -f "$DEPLOY_ENV" ]]; then
  # shellcheck disable=SC1090
  set -a
  source "$DEPLOY_ENV"
  set +a
fi

SSH_HOST="${B2B_COMMERCE_SSH:-}"
REMOTE_DIR="${B2B_COMMERCE_REMOTE_DIR:-/opt/b2b-commerce}"
TAG="${B2B_COMMERCE_IMAGE_TAG:-}"

if [[ -z "$SSH_HOST" ]]; then
  echo "ERROR: set B2B_COMMERCE_SSH=user@host or create scripts/deploy.env" >&2
  exit 1
fi
if [[ -z "$TAG" ]]; then
  echo "ERROR: set B2B_COMMERCE_IMAGE_TAG=<git-sha>" >&2
  exit 1
fi

echo "== Deploy ${TAG} on ${SSH_HOST}:${REMOTE_DIR} =="
ssh -o BatchMode=yes "${SSH_HOST}" \
  "cd '${REMOTE_DIR}' && B2B_COMMERCE_IMAGE_TAG='${TAG}' ./scripts/deploy.sh"
echo "== Done =="
