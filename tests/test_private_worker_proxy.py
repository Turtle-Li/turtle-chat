from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path

import pytest

from chatgpt_web_gateway.private_worker_proxy import (
    PrivateWorkerProxyError,
    PrivateWorkerProxySettings,
    manifest_worker_ports,
)


def settings(manifest: Path) -> PrivateWorkerProxySettings:
    return PrivateWorkerProxySettings(
        bind_host="127.0.0.2",
        allowed_networks=(ipaddress.ip_network("127.0.0.0/8"),),
        manifest_path=manifest,
        fixed_ports=frozenset({38330, 38340}),
        primary_worker_port=38320,
        dynamic_worker_port_start=38360,
        dynamic_worker_port_end=40359,
    )


def write_manifest(path: Path, worker_ports: list[int]) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "accounts": {
                    f"account-{index}": {"worker_port": port}
                    for index, port in enumerate(worker_ports)
                },
            }
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def test_manifest_worker_ports_accepts_primary_fixed_and_dynamic_range(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "account-runtimes.json"
    write_manifest(manifest, [38320, 38330, 38360, 38400])

    assert manifest_worker_ports(settings(manifest)) == frozenset(
        {38320, 38330, 38360, 38400}
    )


def test_manifest_worker_ports_rejects_unrelated_host_port(tmp_path: Path) -> None:
    manifest = tmp_path / "account-runtimes.json"
    write_manifest(manifest, [443])

    with pytest.raises(PrivateWorkerProxyError, match="outside"):
        manifest_worker_ports(settings(manifest))


def test_peer_allowlist_is_subnet_scoped(tmp_path: Path) -> None:
    resolved = settings(tmp_path / "unused.json")

    assert resolved.peer_allowed("127.0.0.8") is True
    assert resolved.peer_allowed("172.18.0.5") is False
    assert resolved.peer_allowed("not-an-address") is False


def test_environment_rejects_public_bind_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TURTLE_WORKER_PROXY_BIND_HOST", "8.8.8.8")
    monkeypatch.setenv("TURTLE_WORKER_PROXY_ALLOWED_CIDRS", "172.30.23.0/24")

    with pytest.raises(PrivateWorkerProxyError, match="private or loopback"):
        PrivateWorkerProxySettings.from_env()
