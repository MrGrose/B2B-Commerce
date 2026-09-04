from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PROD = ROOT / "docker-compose.prod.yml"
CADDYFILE = ROOT / "Caddyfile"
PROD_MK = ROOT / "make" / "prod.mk"
ENV_PROD_EXAMPLE = ROOT / ".env.prod.example"
DEPLOY_SCRIPT = ROOT / "scripts" / "deploy.sh"
ROLLBACK_SCRIPT = ROOT / "scripts" / "rollback.sh"


def test_compose_prod_has_no_caddy_service() -> None:
    text = COMPOSE_PROD.read_text(encoding="utf-8")
    assert "\n  caddy:" not in text
    assert "caddy_data" not in text
    assert "caddy_config" not in text


def test_worker_healthcheck_uses_proc_not_pgrep() -> None:
    text = COMPOSE_PROD.read_text(encoding="utf-8")
    worker_block = text.split("worker:", 1)[1].split("\n\n", 1)[0]
    assert "pgrep" not in worker_block
    assert "/proc/1/cmdline" in worker_block


def test_caddyfile_proxies_loopback_with_xff_hardening() -> None:
    text = CADDYFILE.read_text(encoding="utf-8")
    assert "reverse_proxy 127.0.0.1:8000" in text
    assert "reverse_proxy api:8000" not in text
    assert "header_up -X-Forwarded-For" in text
    assert "header_up X-Forwarded-For {remote_host}" in text


def test_prod_edge_health_make_target() -> None:
    text = PROD_MK.read_text(encoding="utf-8")
    assert "prod-edge-health:" in text
    assert 'curl -fsS "https://$${DOMAIN}/api/health"' in text


def test_deploy_scripts_do_not_manage_caddy() -> None:
    for path in (DEPLOY_SCRIPT, ROLLBACK_SCRIPT):
        text = path.read_text(encoding="utf-8").lower()
        assert "caddy" not in text

def test_env_prod_example_includes_docker_bridge_subnet_for_forwarded_allow_ips() -> None:
    text = ENV_PROD_EXAMPLE.read_text(encoding="utf-8")
    assert "FORWARDED_ALLOW_IPS=172.28.10.0/24,127.0.0.1,::1" in text
    assert "172.28.10.0/24" in text
