set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-b2b-commerce-postgres}"
ENV_FILE="${ENV_FILE:-.env}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
MAX_RETRIES="${BACKUP_MAX_RETRIES:-6}"
RETRY_DELAY_SEC="${BACKUP_RETRY_DELAY_SEC:-10}"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="${BACKUP_DIR:-/var/backups/b2b_commerce}"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/b2b_commerce_${STAMP}.sql.gz"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

: "${POSTGRES_USER:?POSTGRES_USER is required (set in $ENV_FILE)}"
: "${POSTGRES_DB:?POSTGRES_DB is required (set in $ENV_FILE)}"

if ! [[ "$MAX_RETRIES" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: BACKUP_MAX_RETRIES must be a positive integer, got: $MAX_RETRIES" >&2
  exit 1
fi
if ! [[ "$RETRY_DELAY_SEC" =~ ^[0-9]+$ ]]; then
  echo "ERROR: BACKUP_RETRY_DELAY_SEC must be a non-negative integer, got: $RETRY_DELAY_SEC" >&2
  exit 1
fi

run_backup_once() {
  docker exec "$POSTGRES_CONTAINER" \
    sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1 || return 1
  docker exec "$POSTGRES_CONTAINER" \
    sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner' | gzip >"$OUT"
}

echo "Starting PostgreSQL backup to $OUT"

attempt=1
while true; do
  if run_backup_once; then
    echo "OK: $OUT"
    break
  fi
  rm -f "$OUT"
  if [[ "$attempt" -ge "$MAX_RETRIES" ]]; then
    echo "ERROR: backup failed after ${MAX_RETRIES} attempt(s)" >&2
    exit 1
  fi
  echo "WARN: backup attempt ${attempt}/${MAX_RETRIES} failed, retrying in ${RETRY_DELAY_SEC}s"
  sleep "$RETRY_DELAY_SEC"
  attempt=$((attempt + 1))
done

deleted=0
while IFS= read -r -d '' old; do
  rm -f "$old"
  deleted=$((deleted + 1))
done < <(find "$OUT_DIR" -maxdepth 1 -type f -name 'b2b_commerce_*.sql.gz' -mtime +"${RETENTION_DAYS}" -print0 2>/dev/null || true)

if [[ "$deleted" -gt 0 ]]; then
  echo "Pruned $deleted backup(s) older than ${RETENTION_DAYS} day(s) in $OUT_DIR"
fi
