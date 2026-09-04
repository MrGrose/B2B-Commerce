from pathlib import Path

DEPLOY_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh"


def _position(text: str, needle: str) -> int:
    pos = text.find(needle)
    assert pos != -1, f"missing marker: {needle!r}"
    return pos


def test_deploy_script_runs_git_pull_before_backup_and_backup_before_migrate() -> None:
    text = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    disk_check = _position(text, "require_disk_space")
    prev_tag = _position(text, 'PREV_TAG="$(tr -d')
    git_pull = _position(text, 'git -C "$ROOT" pull --ff-only')
    backup = _position(text, "./scripts/backup-postgres.sh")
    migrate = _position(text, "alembic upgrade head")

    assert disk_check < prev_tag < git_pull < backup < migrate
