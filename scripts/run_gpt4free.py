from __future__ import annotations

import os
import subprocess
from pathlib import Path


PINNED_COMMIT = "48d845749f5b4c0148a9188f33ae498f1f41c21d"
OVERLAY_PATH = Path("patches/gpt4free-openaiaccount-gpt56.patch")
OVERLAY_FILES = {
    "g4f/api/__init__.py",
    "g4f/client/__init__.py",
    "g4f/Provider/openai/models.py",
    "g4f/Provider/needs_auth/OpenaiChat.py",
    "g4f/Provider/openai/media_pump.py",
    "g4f/providers/base_provider.py",
    "g4f/providers/tool_support.py",
    "g4f/requests/__init__.py",
    "g4f/client/stubs.py",
    "g4f/tools/run_tools.py",
}
ALLOWED_ENV_NAMES = {
    "UPSTREAM_API_KEY",
    "GPT4FREE_AUTH_DIR",
    "GPT4FREE_BIND_HOST",
    "GPT4FREE_PORT",
    "GPT4FREE_PROVIDER",
    "GPT4FREE_MODEL",
    "GPT4FREE_BROWSER_HOST",
    "GPT4FREE_BROWSER_PORT",
    "TURTLE_MEDIA_STRICT",
    "TURTLE_MEDIA_PUMP_URL",
    "TURTLE_MEDIA_PUMP_SECRET",
    "TURTLE_MEDIA_PUMP_TIMEOUT_SECONDS",
    "TURTLE_MEDIA_PUMP_RETRY_ATTEMPTS",
    "TURTLE_MEDIA_PUMP_MAX_INPUT_BYTES",
    "TURTLE_MEDIA_UPLOAD_CONCURRENCY",
    "TURTLE_MEDIA_AUTH_TTL_SECONDS",
}


def load_known_env(env_path: Path) -> str:
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
        result = value.strip().strip('"').strip("'")
        os.environ.setdefault(name, result)
    upstream_key = os.getenv("UPSTREAM_API_KEY", "").strip()
    if not upstream_key:
        raise SystemExit("UPSTREAM_API_KEY is missing from .env")
    return upstream_key


def positive_port(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not 1 <= value <= 65535:
        raise SystemExit(f"{name} must be between 1 and 65535")
    return value


def _git(source: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(source), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def apply_reviewed_overlay(source: Path, patch_path: Path) -> None:
    if not patch_path.is_file():
        raise SystemExit(f"Reviewed gpt4free overlay is missing: {patch_path}")

    status = _git(source, "status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0:
        raise SystemExit("Unable to inspect the pinned gpt4free worktree")
    changed = {line[3:] for line in status.stdout.splitlines() if len(line) > 3}
    if changed and changed != OVERLAY_FILES:
        raise SystemExit("Pinned gpt4free source has changes outside the reviewed overlay")

    reverse_check = _git(source, "apply", "--reverse", "--check", str(patch_path))
    if reverse_check.returncode == 0:
        return
    if changed:
        raise SystemExit("Pinned gpt4free source does not match the reviewed GPT-5.6 overlay")

    apply_check = _git(source, "apply", "--check", str(patch_path))
    if apply_check.returncode != 0:
        raise SystemExit("Reviewed GPT-5.6 overlay no longer applies to the pinned source")
    applied = _git(source, "apply", str(patch_path))
    if applied.returncode != 0:
        raise SystemExit("Failed to apply the reviewed GPT-5.6 overlay")

    verified = _git(source, "apply", "--reverse", "--check", str(patch_path))
    if verified.returncode != 0:
        raise SystemExit("Applied GPT-5.6 overlay failed its verification check")


def secure_auth_permissions(auth_dir: Path) -> None:
    auth_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if auth_dir.is_symlink():
        raise SystemExit("GPT4FREE_AUTH_DIR must not be a symlink")

    for current_root, directory_names, file_names in os.walk(auth_dir, followlinks=False):
        current_path = Path(current_root)
        current_path.chmod(0o700)
        for directory_name in directory_names:
            directory_path = current_path / directory_name
            if directory_path.is_symlink():
                raise SystemExit("GPT4FREE_AUTH_DIR must not contain symlinks")
            directory_path.chmod(0o700)
        for file_name in file_names:
            file_path = current_path / file_name
            if file_path.is_symlink():
                raise SystemExit("GPT4FREE_AUTH_DIR must not contain symlinks")
            if file_path.is_file():
                file_path.chmod(0o600)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    upstream_key = load_known_env(root / ".env")
    source = root / ".runtime" / "gpt4free-src"
    cli = root / ".runtime" / "gpt4free-venv" / "bin" / "g4f"
    auth_dir = Path(
        os.getenv("GPT4FREE_AUTH_DIR", str(root / ".runtime" / "gpt4free-auth"))
    ).expanduser().resolve()

    if not source.is_dir() or not (source / ".git").is_dir():
        raise SystemExit("Pinned gpt4free source is missing; follow the README installation steps")
    if not cli.is_file():
        raise SystemExit("gpt4free virtual environment is missing; follow the README installation steps")
    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if revision != PINNED_COMMIT:
        raise SystemExit("gpt4free source revision does not match the reviewed pin")
    apply_reviewed_overlay(source, root / OVERLAY_PATH)

    secure_auth_permissions(auth_dir)

    bind_host = os.getenv("GPT4FREE_BIND_HOST", "127.0.0.1").strip()
    if bind_host not in {"127.0.0.1", "::1"}:
        raise SystemExit("GPT4FREE_BIND_HOST must remain loopback-only")

    port = positive_port("GPT4FREE_PORT", 8320)
    browser_port = positive_port("GPT4FREE_BROWSER_PORT", 9223)
    browser_host = os.getenv("GPT4FREE_BROWSER_HOST", "127.0.0.1").strip()
    if browser_host not in {"127.0.0.1", "::1"}:
        raise SystemExit("GPT4FREE_BROWSER_HOST must remain loopback-only")

    environment = os.environ.copy()
    environment["G4F_API_KEY"] = upstream_key
    environment["PYTHONUNBUFFERED"] = "1"
    os.umask(0o077)

    arguments = [
        str(cli),
        "api",
        "--bind",
        f"{bind_host}:{port}",
        "--no-gui",
        "--provider",
        os.getenv("GPT4FREE_PROVIDER", "OpenaiAccount"),
        "--model",
        os.getenv("GPT4FREE_MODEL", "auto"),
        "--cookies-dir",
        str(auth_dir),
        "--browser-port",
        str(browser_port),
        "--browser-host",
        browser_host,
        "--disable-pa-auto-download",
        "--no-access-log",
        "--disable-colors",
    ]
    os.execve(str(cli), arguments, environment)


if __name__ == "__main__":
    main()
