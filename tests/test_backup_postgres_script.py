import re
from pathlib import Path

BACKUP_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup-postgres.sh"


def test_backup_postgres_docker_exec_has_no_t_flag() -> None:
    text = BACKUP_SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"docker exec\s+-T\b", text) is None
    assert text.count('docker exec "$POSTGRES_CONTAINER"') == 2
    assert "pg_isready" in text
    assert "pg_dump" in text
