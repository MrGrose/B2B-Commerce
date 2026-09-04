set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DISK_THRESHOLD_PCT="${DISK_THRESHOLD_PCT:-80}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/b2b_commerce}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
DISK_MOUNT="${DISK_MOUNT:-/}"

usage_pct="$(df -P "$DISK_MOUNT" | awk 'NR==2 {gsub(/%/,"",$5); print $5}')"
if [[ -z "$usage_pct" ]]; then
  echo "ERROR: cannot read disk usage for $DISK_MOUNT" >&2
  exit 1
fi

echo "Disk usage on $DISK_MOUNT: ${usage_pct}% (threshold ${DISK_THRESHOLD_PCT}%)"

if [[ "$usage_pct" -lt "$DISK_THRESHOLD_PCT" ]]; then
  echo "OK: below threshold, no cleanup"
  exit 0
fi

echo "WARN: disk at or above ${DISK_THRESHOLD_PCT}%, starting cleanup"

pruned_backups=0
if [[ -d "$BACKUP_DIR" ]]; then
  while IFS= read -r -d '' old; do
    rm -f "$old"
    pruned_backups=$((pruned_backups + 1))
  done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'b2b_commerce_*.sql.gz' -mtime +"${BACKUP_RETENTION_DAYS}" -print0 2>/dev/null || true)
fi

echo "Pruned ${pruned_backups} backup file(s) from $BACKUP_DIR"

docker image prune -f >/dev/null 2>&1 || true
docker builder prune -f --filter 'until=168h' >/dev/null 2>&1 || true

echo "Cleanup finished"
