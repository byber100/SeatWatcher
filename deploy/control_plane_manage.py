from __future__ import annotations

import getpass
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE_NAME = "seatwatcher-control"
SERVICE_FILE = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")
CONTROL_VENV = ROOT / ".control-venv"
CONTROL_PYTHON = CONTROL_VENV / "bin" / "python"
RUNTIME = ROOT / ".runtime" / "control"
CONFIG_PATH = RUNTIME / "config.json"
RCLONE_CONFIG = RUNTIME / "rclone.conf"
REQUIREMENTS = ROOT / "deploy" / "control_requirements.txt"


def run(*args: str, sudo: bool = False) -> None:
    command = [*args]
    if sudo:
        command.insert(0, "sudo")
    subprocess.run(command, check=True)


def default_config() -> dict:
    return {
        "remote_name": "seatwatcher-drive",
        "remote_folder": "ChatGPT/SeatWatcher Control",
        "sheet_export_file": "SeatWatcher Control.xlsx",
        "poll_seconds": 30,
        "health_upload_seconds": 60,
        "protection_marker_max_age_seconds": 900,
        "rclone_config": str(RCLONE_CONFIG),
    }


def ensure_runtime_config() -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(default_config(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    CONFIG_PATH.chmod(0o600)
    if RCLONE_CONFIG.exists():
        RCLONE_CONFIG.chmod(0o600)


def build_service_unit(user_name: str | None = None) -> str:
    user = user_name or getpass.getuser()
    return f"""[Unit]
Description=SeatWatcher Google Drive mobile control plane
After=network-online.target seatwatcher.service
Wants=network-online.target

[Service]
Type=simple
User={user}
WorkingDirectory={ROOT}
Environment=PYTHONUNBUFFERED=1
ExecStart={CONTROL_PYTHON} {ROOT / 'control_plane.py'}
Restart=always
RestartSec=15
UMask=0077

[Install]
WantedBy=multi-user.target
"""


def install() -> None:
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11+ is required")
    run("apt-get", "update", sudo=True)
    run(
        "apt-get",
        "install",
        "-y",
        "python3",
        "python3-venv",
        "python3-pip",
        "rclone",
        sudo=True,
    )
    if not CONTROL_PYTHON.exists():
        run("python3", "-m", "venv", str(CONTROL_VENV))
    run(str(CONTROL_PYTHON), "-m", "pip", "install", "--upgrade", "pip")
    run(str(CONTROL_PYTHON), "-m", "pip", "install", "-r", str(REQUIREMENTS))
    run(str(CONTROL_PYTHON), "-m", "playwright", "install", "chromium")
    ensure_runtime_config()

    temp = RUNTIME / f"{SERVICE_NAME}.service"
    temp.write_text(build_service_unit(), encoding="utf-8")
    run("cp", str(temp), str(SERVICE_FILE), sudo=True)
    run("systemctl", "daemon-reload", sudo=True)
    run("systemctl", "enable", "--now", SERVICE_NAME, sudo=True)
    run("systemctl", "--no-pager", "--full", "status", SERVICE_NAME, sudo=True)



def auth_drive() -> None:
    ensure_runtime_config()
    print("Google Drive OAuth를 OCI 내부 rclone 설정에 연결합니다.")
    print("브라우저 승인이 끝나면 토큰은 이 VM의 .runtime/control/rclone.conf에만 저장됩니다.")
    run(
        "rclone",
        "config",
        "create",
        "seatwatcher-drive",
        "drive",
        "scope=drive",
        "--config",
        str(RCLONE_CONFIG),
    )
    RCLONE_CONFIG.chmod(0o600)
    run("systemctl", "restart", SERVICE_NAME, sudo=True)
    print("Drive OAuth 저장 완료. seatwatcher-control 서비스를 재시작했습니다.")


def service(action: str) -> None:
    if action == "logs":
        run("journalctl", "-u", SERVICE_NAME, "-n", "200", "-f", sudo=True)
        return
    if action == "status":
        run("systemctl", "--no-pager", "--full", "status", SERVICE_NAME, sudo=True)
        return
    run("systemctl", action, SERVICE_NAME, sudo=True)


def once() -> None:
    ensure_runtime_config()
    run(str(CONTROL_PYTHON), str(ROOT / "control_plane.py"), "--once")


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "install":
        install()
    elif action == "auth-drive":
        auth_drive()
    elif action in {"start", "stop", "restart", "status", "logs"}:
        service(action)
    elif action == "once":
        once()
    else:
        print(
            "usage: python3 deploy/control_plane_manage.py "
            "{install|auth-drive|start|stop|restart|status|logs|once}"
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
