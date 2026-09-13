from __future__ import annotations

import getpass
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE_NAME = "seatwatcher"
SERVICE_FILE = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"


def run(*args: str, sudo: bool = False) -> None:
    command = [*args]
    if sudo:
        command.insert(0, "sudo")
    subprocess.run(command, check=True)


def require_private_files() -> None:
    required = [
        ROOT / ".env.local",
        ROOT / "watch_targets.local.json",
        ROOT / ".runtime" / "kakao_tokens.json",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required if not path.exists()]
    if missing:
        raise SystemExit("missing private files: " + ", ".join(missing))
    for path in required:
        path.chmod(0o600)


def build_service_unit(user_name: str | None = None) -> str:
    user = user_name or getpass.getuser()
    return f"""[Unit]
Description=SeatWatcher cancellation-seat monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
WorkingDirectory={ROOT}
Environment=PYTHONUNBUFFERED=1
ExecStart={VENV_PYTHON} {ROOT / 'watcher.py'} --watch --notify
Restart=always
RestartSec=15
UMask=0077

[Install]
WantedBy=multi-user.target
"""


def install() -> None:
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11+ is required. Use Ubuntu 24.04 or newer for the SeatWatcher VM.")
    run("apt-get", "update", sudo=True)
    run("apt-get", "install", "-y", "python3", "python3-venv", "python3-pip", "git", "curl", sudo=True)
    if not VENV_PYTHON.exists():
        run("python3", "-m", "venv", str(ROOT / ".venv"))
    run(str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip")
    run(str(VENV_PYTHON), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt"))
    require_private_files()

    unit = build_service_unit()
    temp = ROOT / ".runtime" / "seatwatcher.service"
    temp.parent.mkdir(parents=True, exist_ok=True)
    temp.write_text(unit, encoding="utf-8")
    run("cp", str(temp), str(SERVICE_FILE), sudo=True)
    run("systemctl", "daemon-reload", sudo=True)
    run("systemctl", "enable", "--now", SERVICE_NAME, sudo=True)
    run("systemctl", "--no-pager", "--full", "status", SERVICE_NAME, sudo=True)


def update() -> None:
    run("git", "-C", str(ROOT), "pull", "--ff-only")
    run(str(VENV_PYTHON), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt"))
    run("systemctl", "restart", SERVICE_NAME, sudo=True)


def service(action: str) -> None:
    if action == "logs":
        run("journalctl", "-u", SERVICE_NAME, "-n", "200", "-f", sudo=True)
        return
    if action == "status":
        run("systemctl", "--no-pager", "--full", "status", SERVICE_NAME, sudo=True)
        return
    run("systemctl", action, SERVICE_NAME, sudo=True)


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "install":
        install()
    elif action == "update":
        update()
    elif action in {"start", "stop", "restart", "status", "logs"}:
        service(action)
    else:
        print("usage: python3 deploy/cloud_vm_manage.py {install|start|stop|restart|status|logs|update}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
