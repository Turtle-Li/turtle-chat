from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CADDY_SITE = ROOT / "deploy" / "turtle-gpt" / "Caddyfile.site"
ROUTER_TEMPLATE = ROOT / "deploy" / "turtle-gpt" / "router.conf.template"


def _site_block(config: str, hostname: str) -> str:
    start = config.index(f"{hostname} {{")
    depth = 0
    for index in range(start, len(config)):
        if config[index] == "{":
            depth += 1
        elif config[index] == "}":
            depth -= 1
            if depth == 0:
                return config[start : index + 1]
    raise AssertionError(f"unterminated Caddy site block for {hostname}")


def _nginx_server_block(config: str, server_name: str) -> str:
    marker = f"    server_name {server_name};"
    marker_index = config.index(marker)
    start = config.rfind("server {", 0, marker_index)
    assert start >= 0
    depth = 0
    for index in range(start, len(config)):
        if config[index] == "{":
            depth += 1
        elif config[index] == "}":
            depth -= 1
            if depth == 0:
                return config[start : index + 1]
    raise AssertionError(f"unterminated nginx server block for {server_name}")


def test_direct_project_api_caddy_site_is_streaming_only_and_uncompressed() -> None:
    config = CADDY_SITE.read_text(encoding="utf-8")
    site = _site_block(config, "api.chat.turtleligpt.com")

    assert "@project_api path /v1/*" in site
    assert "reverse_proxy 172.18.0.1:33010" in site
    assert "header_up X-Turtle-Direct-Client-IP {remote_host}" in site
    assert "header_up Accept-Encoding identity" in site
    assert "flush_interval -1" in site
    assert "encode " not in site
    assert "Cache-Control \"no-store\"" in site
    assert '"type":"invalid_request_error"' in site


def test_direct_project_api_router_keeps_the_web_ui_as_the_default_server() -> None:
    config = ROUTER_TEMPLATE.read_text(encoding="utf-8")

    assert config.index("    server_name _;") < config.index(
        "    server_name api.chat.turtleligpt.com;"
    )


def test_direct_project_api_router_reuses_connections_without_buffering() -> None:
    config = ROUTER_TEMPLATE.read_text(encoding="utf-8")
    server = _nginx_server_block(config, "api.chat.turtleligpt.com")

    assert "location ^~ /v1/" in server
    assert "rewrite ^/v1/(.*)$ /api/project/v1/$1 break;" in server
    assert "proxy_set_header Connection \"\";" in server
    assert "proxy_set_header Accept-Encoding identity;" in server
    assert "proxy_request_buffering off;" in server
    assert "proxy_buffering off;" in server
    assert 'add_header X-Accel-Buffering "no" always;' in server
    assert "proxy_set_header X-Turtle-Project-External-Prefix /v1;" in server


def test_direct_project_api_edge_limits_reject_without_queueing() -> None:
    config = ROUTER_TEMPLATE.read_text(encoding="utf-8")
    server = _nginx_server_block(config, "api.chat.turtleligpt.com")

    assert (
        "limit_req_zone $turtle_direct_api_client "
        "zone=turtle_direct_api_rate:10m rate=30r/s;"
    ) in config
    assert "limit_req zone=turtle_direct_api_rate burst=90 nodelay;" in server
    assert "limit_conn turtle_direct_api_connections 64;" in server
    assert "limit_req_status 429;" in server
    assert "limit_conn_status 429;" in server
    assert 'add_header Retry-After "1" always;' in server
    assert '"type":"rate_limit_error"' in server
    assert 'proxy_set_header X-Turtle-Direct-Client-IP "";' in server


def test_regular_web_edge_strips_the_internal_project_prefix_header() -> None:
    config = ROUTER_TEMPLATE.read_text(encoding="utf-8")
    default_server = _nginx_server_block(config, "_")

    assert 'proxy_set_header X-Turtle-Project-External-Prefix "";' in default_server
