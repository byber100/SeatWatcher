from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


SEND_URL = "https://api.pushover.net/1/messages.json"
MAX_MESSAGE_LENGTH = 1024
MAX_TITLE_LENGTH = 250
MAX_URL_TITLE_LENGTH = 100
MAX_URL_LENGTH = 512
ALLOWED_SOUNDS = {"vibrate", "none"}


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
        raise ValueError("Pushover sound는 vibrate 또는 none만 허용합니다.")

    form = {
        "token": _env("SEATWATCHER_PUSHOVER_APP_TOKEN", required=True),
        "user": _env("SEATWATCHER_PUSHOVER_USER_KEY", required=True),
        "message": message,
        "title": title[:MAX_TITLE_LENGTH],
        "url": link_url,
        "url_title": "예매 확인"[:MAX_URL_TITLE_LENGTH],
        "priority": "0",
        "sound": sound_value,
    }
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
) -> dict[str, Any]:
    payload = _post_form(
        SEND_URL,
        build_message_form(text, link_url=link_url, sound=sound, title=title),
    )
    if int(payload.get("status") or 0) != 1:
        raise RuntimeError(f"Pushover 메시지 발송 실패: {payload}")
    return payload
