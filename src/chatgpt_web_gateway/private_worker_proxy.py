"""Private Docker-bridge proxy for loopback-only account workers.

Linux containers cannot reach services bound to the host's loopback address via
``host.docker.internal``.  This process exposes only the sanitized runtime
ports on Docker's host bridge and accepts connections solely from the Turtle
Compose subnet.  Credentials and browser state never pass through its control
plane; it is a byte-for-byte TCP relay to the corresponding loopback port.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PORT_LIST_RE = re.compile(r"^[0-9, -]+$")


class PrivateWorkerProxyError(RuntimeError):
    pass


def _port(value: Any, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise PrivateWorkerProxyError(f"{field} is invalid") from exc
    if not 1 <= result <= 65535:
        raise PrivateWorkerProxyError(f"{field} is invalid")
    return result


def _ports(value: str) -> frozenset[int]:
    normalized = str(value or "").strip()
    if not normalized:
        return frozenset()
    if not PORT_LIST_RE.fullmatch(normalized):
        raise PrivateWorkerProxyError("fixed port list is invalid")
    result: set[int] = set()
    for item in normalized.split(","):
        part = item.strip()
        if not part:
            continue
        if "-" not in part:
            result.add(_port(part, "fixed port"))
            continue
        start_text, end_text = part.split("-", 1)
        start = _port(start_text.strip(), "fixed port range")
        end = _port(end_text.strip(), "fixed port range")
        if start > end or end - start > 4096:
            raise PrivateWorkerProxyError("fixed port range is invalid")
        result.update(range(start, end + 1))
    return frozenset(result)


def _networks(value: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    result = []
    for item in str(value or "").split(","):
        normalized = item.strip()
        if not normalized:
            continue
        try:
            network = ipaddress.ip_network(normalized, strict=True)
        except ValueError as exc:
            raise PrivateWorkerProxyError("allowed proxy subnet is invalid") from exc
        if not network.is_private:
            raise PrivateWorkerProxyError("allowed proxy subnet must be private")
        result.append(network)
    if not result:
        raise PrivateWorkerProxyError("at least one allowed proxy subnet is required")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class PrivateWorkerProxySettings:
    bind_host: str
    allowed_networks: tuple[
        ipaddress.IPv4Network | ipaddress.IPv6Network, ...
    ]
    manifest_path: Path
    fixed_ports: frozenset[int]
    primary_worker_port: int
    dynamic_worker_port_start: int
    dynamic_worker_port_end: int
    refresh_seconds: float = 1.0

    @classmethod
    def from_env(cls) -> "PrivateWorkerProxySettings":
        bind_host = os.getenv("TURTLE_WORKER_PROXY_BIND_HOST", "").strip()
        try:
            bind_address = ipaddress.ip_address(bind_host)
        except ValueError as exc:
            raise PrivateWorkerProxyError("proxy bind host is invalid") from exc
        if bind_address.is_unspecified or bind_address.is_multicast or bind_address.is_global:
            raise PrivateWorkerProxyError("proxy bind host must be a private or loopback address")
        primary = _port(
            os.getenv("TURTLE_WORKER_PROXY_PRIMARY_PORT", "38320"),
            "primary worker port",
        )
        dynamic_start = _port(
            os.getenv("TURTLE_WORKER_PROXY_DYNAMIC_PORT_START", "38360"),
            "dynamic worker port start",
        )
        dynamic_end = _port(
            os.getenv("TURTLE_WORKER_PROXY_DYNAMIC_PORT_END", "40359"),
            "dynamic worker port end",
        )
        if dynamic_start > dynamic_end or dynamic_end - dynamic_start > 4096:
            raise PrivateWorkerProxyError("dynamic worker port range is invalid")
        refresh_seconds = float(
            os.getenv("TURTLE_WORKER_PROXY_REFRESH_SECONDS", "1")
        )
        if not 0.2 <= refresh_seconds <= 30:
            raise PrivateWorkerProxyError("proxy refresh interval is invalid")
        return cls(
            bind_host=str(bind_address),
            allowed_networks=_networks(
                os.getenv("TURTLE_WORKER_PROXY_ALLOWED_CIDRS", "")
            ),
            manifest_path=Path(
                os.getenv(
                    "TURTLE_LOGIN_RUNTIME_MANIFEST",
                    ".runtime/account-runtimes.json",
                )
            ).expanduser().resolve(),
            fixed_ports=_ports(
                os.getenv("TURTLE_WORKER_PROXY_FIXED_PORTS", "38330,38340")
            ),
            primary_worker_port=primary,
            dynamic_worker_port_start=dynamic_start,
            dynamic_worker_port_end=dynamic_end,
            refresh_seconds=refresh_seconds,
        )

    def worker_port_allowed(self, port: int) -> bool:
        return port in self.fixed_ports or port == self.primary_worker_port or (
            self.dynamic_worker_port_start <= port <= self.dynamic_worker_port_end
        )

    def peer_allowed(self, raw_host: str) -> bool:
        try:
            address = ipaddress.ip_address(raw_host)
        except ValueError:
            return False
        return any(address in network for network in self.allowed_networks)


def manifest_worker_ports(settings: PrivateWorkerProxySettings) -> frozenset[int]:
    path = settings.manifest_path
    try:
        info = path.lstat()
    except OSError as exc:
        raise PrivateWorkerProxyError("account runtime manifest is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise PrivateWorkerProxyError("account runtime manifest must be a regular file")
    if info.st_size > 64 * 1024:
        raise PrivateWorkerProxyError("account runtime manifest is too large")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PrivateWorkerProxyError("account runtime manifest is invalid") from exc
    accounts = payload.get("accounts") if isinstance(payload, dict) else None
    if payload.get("version") not in {1, 2} or not isinstance(accounts, dict):
        raise PrivateWorkerProxyError("account runtime manifest is invalid")
    ports: set[int] = set()
    for raw in accounts.values():
        if not isinstance(raw, dict):
            raise PrivateWorkerProxyError("account runtime manifest is invalid")
        port = _port(raw.get("worker_port"), "account worker port")
        if not settings.worker_port_allowed(port):
            raise PrivateWorkerProxyError("account worker port is outside the deployment range")
        ports.add(port)
    return frozenset(ports)


async def _copy_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(64 * 1024):
        writer.write(data)
        await writer.drain()


async def relay_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    settings: PrivateWorkerProxySettings,
    target_port: int,
) -> None:
    peer = client_writer.get_extra_info("peername")
    peer_host = str(peer[0]) if isinstance(peer, tuple) and peer else ""
    if not settings.peer_allowed(peer_host):
        client_writer.close()
        await client_writer.wait_closed()
        return
    try:
        target_reader, target_writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", target_port),
            timeout=3.0,
        )
    except (TimeoutError, OSError):
        client_writer.close()
        await client_writer.wait_closed()
        return
    tasks = {
        asyncio.create_task(_copy_stream(client_reader, target_writer)),
        asyncio.create_task(_copy_stream(target_reader, client_writer)),
    }
    _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    target_writer.close()
    client_writer.close()
    await asyncio.gather(
        target_writer.wait_closed(),
        client_writer.wait_closed(),
        return_exceptions=True,
    )


async def run_proxy(settings: PrivateWorkerProxySettings) -> None:
    servers: dict[int, asyncio.AbstractServer] = {}
    try:
        while True:
            desired = settings.fixed_ports | manifest_worker_ports(settings)
            for port in sorted(set(servers) - set(desired)):
                server = servers.pop(port)
                server.close()
                await server.wait_closed()
            for port in sorted(set(desired) - set(servers)):
                servers[port] = await asyncio.start_server(
                    lambda reader, writer, selected=port: relay_connection(
                        reader,
                        writer,
                        settings=settings,
                        target_port=selected,
                    ),
                    host=settings.bind_host,
                    port=port,
                    start_serving=True,
                )
            await asyncio.sleep(settings.refresh_seconds)
    finally:
        for server in servers.values():
            server.close()
        await asyncio.gather(
            *(server.wait_closed() for server in servers.values()),
            return_exceptions=True,
        )


def main() -> None:
    asyncio.run(run_proxy(PrivateWorkerProxySettings.from_env()))


if __name__ == "__main__":
    main()
