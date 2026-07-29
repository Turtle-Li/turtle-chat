"""Dependency-free runtime resource sampling for the operations console."""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _read_int(path: Path) -> int | None:
    value = _read_text(path)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ResourceMonitor:
    def __init__(self, interval_seconds: int = 5) -> None:
        self.interval_seconds = max(2, int(interval_seconds))
        self.proc_root = Path(os.getenv("TURTLE_MONITOR_PROC_ROOT", "/proc"))
        self.cgroup_root = Path(os.getenv("TURTLE_MONITOR_CGROUP_ROOT", "/sys/fs/cgroup"))
        self.disk_path = Path(os.getenv("TURTLE_MONITOR_DISK_PATH", "/app/backend/data"))
        self.scope = str(os.getenv("TURTLE_MONITOR_SCOPE", "")).strip().lower() or (
            "container" if Path("/.dockerenv").exists() else "host"
        )
        self._samples: deque[dict[str, Any]] = deque(
            maxlen=max(720, int(24 * 60 * 60 / self.interval_seconds))
        )
        self._lock = threading.RLock()
        self._started = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._previous_cpu: tuple[int, float] | None = None
        self._previous_proc_cpu: tuple[int, int] | None = None
        self._previous_network: tuple[int, int, float] | None = None

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._thread = threading.Thread(
                target=self._run,
                name="turtle-resource-monitor",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            sample = self._sample()
            with self._lock:
                self._samples.append(sample)
            self._stop.wait(self.interval_seconds)

    def _cgroup_cpu(self, now: float) -> float | None:
        stat = _read_text(self.cgroup_root / "cpu.stat")
        usage_usec = None
        for line in stat.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "usage_usec":
                try:
                    usage_usec = int(parts[1])
                except ValueError:
                    pass
        if usage_usec is None:
            return None
        quota_cores = float(os.cpu_count() or 1)
        maximum = _read_text(self.cgroup_root / "cpu.max").split()
        if len(maximum) == 2 and maximum[0] != "max":
            try:
                quota_cores = max(0.01, int(maximum[0]) / int(maximum[1]))
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        current = (usage_usec, now)
        previous = self._previous_cpu
        self._previous_cpu = current
        if previous is None or now <= previous[1]:
            return 0.0
        used_seconds = (usage_usec - previous[0]) / 1_000_000
        elapsed = now - previous[1]
        return max(0.0, min(100.0, used_seconds / elapsed / quota_cores * 100))

    def _proc_cpu(self) -> float:
        parts = _read_text(self.proc_root / "stat").splitlines()
        if not parts or not parts[0].startswith("cpu "):
            return 0.0
        try:
            values = [int(value) for value in parts[0].split()[1:]]
        except ValueError:
            return 0.0
        total = sum(values)
        idle = sum(values[index] for index in (3, 4) if index < len(values))
        previous = self._previous_proc_cpu
        self._previous_proc_cpu = (total, idle)
        if previous is None or total <= previous[0]:
            return 0.0
        total_delta = total - previous[0]
        idle_delta = idle - previous[1]
        return max(0.0, min(100.0, (total_delta - idle_delta) / total_delta * 100))

    def _memory(self) -> tuple[int, int | None]:
        current = _read_int(self.cgroup_root / "memory.current")
        maximum_text = _read_text(self.cgroup_root / "memory.max")
        maximum = None
        if maximum_text and maximum_text != "max":
            try:
                maximum = int(maximum_text)
            except ValueError:
                pass
        if current is not None and maximum is not None:
            return max(0, current), max(0, maximum)

        values: dict[str, int] = {}
        for line in _read_text(self.proc_root / "meminfo").splitlines():
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            try:
                values[key] = int(raw.strip().split()[0]) * 1024
            except (ValueError, IndexError):
                continue
        total = values.get("MemTotal")
        available = values.get("MemAvailable", values.get("MemFree", 0))
        return max(0, int((total or 0) - available)), total

    def _network(self, now: float) -> tuple[int, int, float, float]:
        received = 0
        transmitted = 0
        for line in _read_text(self.proc_root / "net/dev").splitlines()[2:]:
            if ":" not in line:
                continue
            interface, raw = line.split(":", 1)
            if interface.strip() == "lo":
                continue
            fields = raw.split()
            try:
                received += int(fields[0])
                transmitted += int(fields[8])
            except (ValueError, IndexError):
                continue
        rx_bps = 0.0
        tx_bps = 0.0
        previous = self._previous_network
        if previous is not None and now > previous[2]:
            elapsed = now - previous[2]
            rx_bps = max(0.0, (received - previous[0]) * 8 / elapsed)
            tx_bps = max(0.0, (transmitted - previous[1]) * 8 / elapsed)
        self._previous_network = (received, transmitted, now)
        return received, transmitted, rx_bps, tx_bps

    def _disk(self) -> tuple[int, int]:
        target = self.disk_path if self.disk_path.exists() else Path("/")
        try:
            stat = os.statvfs(target)
            total = int(stat.f_blocks * stat.f_frsize)
            available = int(stat.f_bavail * stat.f_frsize)
            return max(0, total - available), max(0, total)
        except OSError:
            return 0, 0

    def _load(self) -> tuple[float | None, float | None, float | None]:
        values = _read_text(self.proc_root / "loadavg").split()
        try:
            return float(values[0]), float(values[1]), float(values[2])
        except (ValueError, IndexError):
            return None, None, None

    def _sample(self) -> dict[str, Any]:
        now = time.time()
        cpu = self._cgroup_cpu(now)
        if cpu is None:
            cpu = self._proc_cpu()
        memory_used, memory_limit = self._memory()
        disk_used, disk_total = self._disk()
        rx_bytes, tx_bytes, rx_bps, tx_bps = self._network(now)
        load1, load5, load15 = self._load()
        return {
            "at": int(now),
            "cpu_percent": round(cpu, 2),
            "memory_used_bytes": memory_used,
            "memory_limit_bytes": memory_limit,
            "memory_percent": (
                round(memory_used / memory_limit * 100, 2) if memory_limit else None
            ),
            "disk_used_bytes": disk_used,
            "disk_total_bytes": disk_total,
            "disk_percent": round(disk_used / disk_total * 100, 2) if disk_total else None,
            "network_rx_bytes": rx_bytes,
            "network_tx_bytes": tx_bytes,
            "network_rx_bps": round(rx_bps, 2),
            "network_tx_bps": round(tx_bps, 2),
            "load_1": load1,
            "load_5": load5,
            "load_15": load15,
        }

    def snapshot(self, hours: int = 1) -> dict[str, Any]:
        self.start()
        with self._lock:
            if not self._samples:
                self._samples.append(self._sample())
            cutoff = int(time.time()) - max(1, int(hours)) * 60 * 60
            samples = [dict(item) for item in self._samples if int(item["at"]) >= cutoff]
        if len(samples) > 240:
            stride = max(1, len(samples) // 240)
            reduced = samples[::stride]
            if reduced[-1] != samples[-1]:
                reduced.append(samples[-1])
            samples = reduced
        current = samples[-1] if samples else self._sample()
        scope_note = (
            "整机只读采样；数值包含 Sub2 等同机服务，不代表 Turtle 独占资源。"
            if self.scope == "host"
            else "当前为 Turtle 应用容器/本机进程可见范围，不包含独立的 Sub2 服务数据。"
        )
        return {
            "scope": self.scope,
            "scope_note": scope_note,
            "sample_interval_seconds": self.interval_seconds,
            "current": current,
            "series": samples,
        }


SYSTEM_MONITOR = ResourceMonitor(
    interval_seconds=int(os.getenv("TURTLE_MONITOR_INTERVAL_SECONDS", "5") or 5)
)
