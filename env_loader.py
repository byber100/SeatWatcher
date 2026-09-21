from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

LEGACY_KORAIL_KEY = "SEATWATCHER_KORAIL_ID"
KORAIL_MEMBER_NO_KEY = "SEATWATCHER_KORAIL_MEMBER_NO"
PROJECT_DIR = Path(__file__).resolve().parent
LOCAL_ENV = PROJECT_DIR / ".env.local"
TEMPLATE_ENV = PROJECT_DIR / ".env"

# OCI의 현재 저장소는 과거 직접 배포 때문에 Git working tree가 dirty라
# fast-forward pull만으로는 운영 코드를 안전하게 갱신할 수 없다.
# 이 브리지는 서비스 시작 직전에 origin/main의 런타임 핵심 파일만 원자적으로
# 동기화하고 한 번 재실행한다. 개인 설정/비밀/상태 파일은 절대 건드리지 않는다.
OCI_DEPLOY_ROOT = Path("/home/ubuntu/SeatWatcher")
OCI_DEPLOY_FILES = ("watcher.py", "standby_reservation.py", "control_plane.py")
OCI_DEPLOY_MARKER = PROJECT_DIR / ".runtime" / "origin_main_runtime_revision.txt"


def _sync_oci_runtime_from_origin_main() -> None:
    """Safely refresh selected runtime files before the watcher starts.

    This bridge exists only for the OCI host whose Git working tree predates the
    canonical main history. It never performs a fetch and never touches config,
    secrets, or runtime state. A failed sync leaves the previous runtime intact.
    """
    if os.name != "posix":
        return
    if PROJECT_DIR != OCI_DEPLOY_ROOT:
        return
    if Path(sys.argv[0]).name != "watcher.py" or "--watch" not in sys.argv:
        return

    try:
        revision = subprocess.run(
            ["git", "-C", str(PROJECT_DIR), "rev-parse", "origin/main"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        if not revision:
            raise RuntimeError("origin/main revision is empty")

        payloads: dict[str, bytes] = {}
        needs_sync = False
        for relative_name in OCI_DEPLOY_FILES:
            completed = subprocess.run(
                ["git", "-C", str(PROJECT_DIR), "show", f"origin/main:{relative_name}"],
                check=True,
                capture_output=True,
                timeout=10,
            )
            if not completed.stdout:
                raise RuntimeError(f"empty deployment payload: {relative_name}")
            payloads[relative_name] = completed.stdout
            target = PROJECT_DIR / relative_name
            if not target.exists() or target.read_bytes() != completed.stdout:
                needs_sync = True

        if OCI_DEPLOY_MARKER.exists():
            current = OCI_DEPLOY_MARKER.read_text(encoding="utf-8").strip()
            if current == revision and not needs_sync:
                return

        # 모든 source payload 확보가 끝난 뒤에만 실제 파일을 바꾼다.
        for relative_name, payload in payloads.items():
            target = PROJECT_DIR / relative_name
            temp = target.with_name(target.name + ".deploytmp")
            temp.write_bytes(payload)
            temp.replace(target)

        OCI_DEPLOY_MARKER.parent.mkdir(parents=True, exist_ok=True)
        OCI_DEPLOY_MARKER.write_text(revision + "\n", encoding="utf-8")
        print(
            f"DEPLOY_SYNC origin_main={revision[:12]} "
            f"files={','.join(OCI_DEPLOY_FILES)}",
            flush=True,
        )

        # 현재 watcher.py는 이미 컴파일되어 실행 중이므로 파일 교체 후
        # 같은 argv로 한 번 재실행해 새 코드를 즉시 메모리에 올린다.
        os.execv(sys.executable, [sys.executable, *sys.argv])
    except Exception as exc:
        print(
            f"WARNING DEPLOY_SYNC skipped error={type(exc).__name__}",
            flush=True,
        )


_sync_oci_runtime_from_origin_main()


def _migrate_legacy_korail_key(env_path: Path) -> None:
    """Rename the old KORAIL ID key without exposing its value."""
    text = env_path.read_text(encoding="utf-8")
    old_pattern = re.compile(rf"^(\s*){re.escape(LEGACY_KORAIL_KEY)}(\s*=)", re.MULTILINE)
    new_pattern = re.compile(rf"^\s*{re.escape(KORAIL_MEMBER_NO_KEY)}\s*=", re.MULTILINE)

    if not old_pattern.search(text):
        return

    if new_pattern.search(text):
        text = re.sub(
            rf"^\s*{re.escape(LEGACY_KORAIL_KEY)}\s*=.*(?:\r?\n|$)",
            "",
            text,
            flags=re.MULTILINE,
        )
    else:
        text = old_pattern.sub(rf"\1{KORAIL_MEMBER_NO_KEY}\2", text)

    env_path.write_text(text, encoding="utf-8")


def _load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    _migrate_legacy_korail_key(env_path)

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not value:
            continue

        # Existing OS variables always win. Because .env.local is loaded first,
        # local secrets also win over any non-secret fallback in the tracked template.
        os.environ.setdefault(key, value)


def load_project_env(path: Path | None = None) -> None:
    """Load local secrets first, then the tracked blank template as fallback."""
    if path is not None:
        _load_env_file(path)
        return

    _load_env_file(LOCAL_ENV)
    _load_env_file(TEMPLATE_ENV)
