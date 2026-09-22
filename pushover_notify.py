from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SEND_URL = "https://api.pushover.net/1/messages.json"
MAX_MESSAGE_LENGTH = 1024
MAX_TITLE_LENGTH = 250
MAX_URL_TITLE_LENGTH = 100
MAX_URL_LENGTH = 512
ALLOWED_SOUNDS = {"vibrate", "persistent", "siren", "pushover", "none"}
NOTIFICATION_HISTORY = Path(__file__).resolve().parent / ".runtime" / "notification_history.jsonl"


def _append_notification_history(payload: dict[str, Any]) -> None:
    try:
        NOTIFICATION_HISTORY.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **payload,
        }
        with NOTIFICATION_HISTORY.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        pass



def _env(name: str, *, required: bool = False) -> str:
    value = os.getenv(name, "").strip()
    if required and not value:
        raise RuntimeError(f"환경변수 {name} 값이 필요합니다.")
    return value


def is_configured() -> bool:
    return bool(
        _env("SEATWATCHER_PUSHOVER_APP_TOKEN")
        and _env("SEATWATCHER_PUSHOVER_USER_KEY")
    )


def _post_form(url: str, form: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Pushover API HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Pushover API 연결 실패: {exc.reason}") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Pushover API JSON 해석 실패: {body[:500]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Pushover API 응답 형식이 예상과 다릅니다.")
    return payload


def build_message_form(
    text: str,
    *,
    link_url: str,
    sound: str,
    title: str = "SeatWatcher",
    priority: int = 0,
    retry: int | None = None,
    expire: int | None = None,
) -> dict[str, str]:
    message = text.strip()
    if not message:
        raise ValueError("Pushover 메시지 본문이 비어 있습니다.")
    if len(message) > MAX_MESSAGE_LENGTH:
        message = message[: MAX_MESSAGE_LENGTH - 1] + "…"
    if not link_url.startswith(("https://", "http://")):
        raise ValueError("Pushover 링크 URL은 http:// 또는 https://로 시작해야 합니다.")
    if len(link_url) > MAX_URL_LENGTH:
        raise ValueError("Pushover 링크 URL은 512자를 넘을 수 없습니다.")
    sound_value = sound.strip()
    if sound_value not in ALLOWED_SOUNDS:
        raise ValueError(f"지원하지 않는 Pushover sound입니다: {sound_value}")
    if priority not in {-2, -1, 0, 1, 2}:
        raise ValueError("Pushover priority는 -2~2 범위여야 합니다.")
    if priority == 2:
        if retry is None or int(retry) < 30:
            raise ValueError("Emergency priority는 retry>=30초가 필요합니다.")
        if expire is None or not (1 <= int(expire) <= 10800):
            raise ValueError("Emergency priority는 expire 1~10800초가 필요합니다.")

    form = {
        "token": _env("SEATWATCHER_PUSHOVER_APP_TOKEN", required=True),
        "user": _env("SEATWATCHER_PUSHOVER_USER_KEY", required=True),
        "message": message,
        "title": title[:MAX_TITLE_LENGTH],
        "url": link_url,
        "url_title": "예매 확인"[:MAX_URL_TITLE_LENGTH],
        "priority": str(priority),
        "sound": sound_value,
    }
    if priority == 2:
        form["retry"] = str(int(retry))
        form["expire"] = str(int(expire))
    device = _env("SEATWATCHER_PUSHOVER_DEVICE")
    if device:
        form["device"] = device
    return form


def send_message(
    text: str,
    *,
    link_url: str,
    sound: str,
    title: str = "SeatWatcher",
    priority: int = 0,
    retry: int | None = None,
    expire: int | None = None,
) -> dict[str, Any]:
    try:
        payload = _post_form(
            SEND_URL,
            build_message_form(
                text,
                link_url=link_url,
                sound=sound,
                title=title,
                priority=priority,
                retry=retry,
                expire=expire,
            ),
        )
        if int(payload.get("status") or 0) != 1:
            raise RuntimeError(f"Pushover 메시지 발송 실패: {payload}")
    except Exception as exc:
        _append_notification_history(
            {
                "status": "failed",
                "title": title[:MAX_TITLE_LENGTH],
                "sound": sound,
                "priority": priority,
                "retry": retry,
                "expire": expire,
                "message": text.strip()[:500],
                "link_url": link_url,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            }
        )
        raise

    _append_notification_history(
        {
            "status": "sent",
            "title": title[:MAX_TITLE_LENGTH],
            "sound": sound,
            "priority": priority,
            "retry": retry,
            "expire": expire,
            "message": text.strip()[:500],
            "link_url": link_url,
            "receipt": str(payload.get("receipt") or ""),
        }
    )
    return payload
