from __future__ import annotations

import importlib.util
import os
from pathlib import Path


ALLOWED_ENV_NAMES = {
    "GATEWAY_API_KEY",
    "UPSTREAM_API_KEY",
    "CLAUDE_WORKER_API_KEY",
    "CLAUDE_AUTH_PATH",
    "CLAUDE_VERIFIED_MODELS_PATH",
    "CLAUDE_BASE_URL",
    "CLAUDE_TIMEOUT_SECONDS",
    "CLAUDE_HEALTH_CACHE_SECONDS",
    "CLAUDE_BIND_HOST",
    "CLAUDE_PORT",
    "CLAUDE_BROWSER_PORT",
    "CLAUDE_PUBLIC_MODEL_NAME",
    "CLAUDE_TIMEZONE",
    "CLAUDE_LOCALE",
    "CLAUDE_PROXY_URL",
}


def load_known_env(env_path: Path) -> None:
    if not env_path.is_file():
        raise SystemExit(".env is missing; create it from .env.example first")
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        name = key.strip()
        if name not in ALLOWED_ENV_NAMES:
            continue
        os.environ.setdefault(name, value.strip().strip('"').strip("'"))
    worker_key = (
        os.getenv("CLAUDE_WORKER_API_KEY", "").strip()
        or os.getenv("UPSTREAM_API_KEY", "").strip()
        or os.getenv("GATEWAY_API_KEY", "").strip()
    )
    if not worker_key:
        raise SystemExit(
            "CLAUDE_WORKER_API_KEY, UPSTREAM_API_KEY or GATEWAY_API_KEY is missing from .env"
        )
    os.environ["CLAUDE_WORKER_API_KEY"] = worker_key


def positive_port(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not 1 <= value <= 65535:
        raise SystemExit(f"{name} must be between 1 and 65535")
    return value


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    load_known_env(root / ".env")
    executable = root / ".venv" / "bin" / "turtle-claude-worker"
    if not executable.is_file():
        raise SystemExit("Claude worker dependencies are missing; run uv sync --extra claude")
    missing_dependencies = [
        name
        for name in ("curl_cffi", "websockets")
        if importlib.util.find_spec(name) is None
    ]
    if missing_dependencies:
        raise SystemExit(
            "Claude worker dependencies are missing; run uv sync --extra claude"
        )

    auth_path = Path(
        os.getenv("CLAUDE_AUTH_PATH", str(root / ".runtime" / "claude-auth" / "session.json"))
    ).expanduser().absolute()
    runtime_root = (root / ".runtime").absolute()
    auth_root = auth_path.parent
    relative_auth_root = None
    try:
        relative_auth_root = auth_root.relative_to(runtime_root)
    except ValueError:
        pass
    allowed_auth_root = auth_root == runtime_root / "claude-auth" or (
        relative_auth_root is not None
        and len(relative_auth_root.parts) == 3
        and relative_auth_root.parts[0] == "accounts"
        and relative_auth_root.parts[2] == "auth"
    )
    if not allowed_auth_root:
        raise SystemExit(
            "CLAUDE_AUTH_PATH must remain in the dedicated Claude runtime directory"
        )
    verified_path = Path(
        os.getenv(
            "CLAUDE_VERIFIED_MODELS_PATH",
            str(auth_root / "verified-models.json"),
        )
    ).expanduser().absolute()
    if verified_path.parent != auth_root:
        raise SystemExit(
            "CLAUDE_VERIFIED_MODELS_PATH must remain in the dedicated Claude auth directory"
        )

    bind_host = os.getenv("CLAUDE_BIND_HOST", "127.0.0.1").strip()
    if bind_host not in {"127.0.0.1", "::1"}:
        raise SystemExit("CLAUDE_BIND_HOST must remain loopback-only")
    positive_port("CLAUDE_PORT", 8330)
    positive_port("CLAUDE_BROWSER_PORT", 9225)
    if os.getenv("CLAUDE_BASE_URL", "https://claude.ai").strip().rstrip("/") != "https://claude.ai":
        raise SystemExit("CLAUDE_BASE_URL must remain exactly https://claude.ai")

    from claude_web_worker.auth import secure_auth_directory

    secure_auth_directory(auth_root)
    os.environ["CLAUDE_AUTH_PATH"] = str(auth_path)
    os.environ["CLAUDE_VERIFIED_MODELS_PATH"] = str(verified_path)
    os.environ["PYTHONUNBUFFERED"] = "1"
    os.umask(0o077)
    environment = os.environ.copy()
    os.execve(str(executable), [str(executable)], environment)


if __name__ == "__main__":
    main()
