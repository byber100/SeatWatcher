from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Iterable


DEFAULT_ALERT_PAGE_URL = "https://byber100.github.io/SeatWatcher/"
ALERT_PAGE_VERSION = "20260913-3"


@dataclass(frozen=True)
class AlertEvent:
    key: str
    alert_type: str
    transport: str
    notification_class: str
    provider: str
    date: str
    departure_time: str
    departure: str
    arrival: str
    departure_code: str
    arrival_code: str
    route: str
    title: str
    current: str
    change: str

    def page_payload(self) -> dict[str, str]:
        data = asdict(self)
        data.pop("key", None)
        data.pop("notification_class", None)
        return data

    def kakao_item(self) -> dict[str, str]:
        icon = "🚄" if self.transport == "기차" else "🚌"
        date_text = f"{self.date[4:6]}/{self.date[6:8]}" if len(self.date) >= 8 else self.date
        time_text = f"{self.departure_time[:2]}:{self.departure_time[2:4]}" if len(self.departure_time) >= 4 else self.departure_time
        return {
            "title": f"{icon} {date_text} {time_text} {self.route}",
            "description": f"[{self.alert_type}] {self.change or self.current}",
        }


def _bus_signature_text(signature: str | None) -> str:
    if not signature or signature == "unavailable":
        return ""
    marker = "remaining="
    if marker not in signature:
        return "예약 가능"
    remaining = signature.split(marker, 1)[1]
    return "예약 가능" if remaining == "?" else f"잔여 {remaining}석"


def _rail_signature_text(signature: str | None) -> str:
    if not signature or signature == "unavailable":
        return ""
    parts = signature.split("|", 2)
    return parts[2] if len(parts) >= 3 else "예약 가능"


def build_bus_event(*, key: str, item: Any, alert_type: str, previous_signature: str | None) -> AlertEvent:
    current = f"잔여 {item.remaining_seats}석" if getattr(item, "remaining_seats", None) is not None else "예약 가능"
    previous = _bus_signature_text(previous_signature)
    change = f"{previous} → {current}" if previous else current
    provider = str(getattr(item, "provider", "") or "")
    schedule = str(getattr(item, "schedule_type", "") or "")
    bus_class = str(getattr(item, "bus_class", "") or "")
    detail = " · ".join(value for value in (provider, bus_class, schedule) if value)
    return AlertEvent(
        key=key,
        alert_type=alert_type,
        transport="버스",
        notification_class="bus",
        provider=provider,
        date=str(getattr(item, "date", "") or ""),
        departure_time=str(getattr(item, "departure_time", "") or ""),
        departure=str(getattr(item, "departure_terminal", "") or ""),
        arrival=str(getattr(item, "arrival_terminal", "") or ""),
        departure_code="",
        arrival_code="",
        route=f"{getattr(item, 'departure_terminal', '')}→{getattr(item, 'arrival_terminal', '')}",
        title=detail,
        current=current,
        change=change,
    )


def build_rail_event(*, key: str, item: Any, alert_type: str, previous_signature: str | None) -> AlertEvent:
    current = str(getattr(item, "seat_text", "") or "예약 가능")
    previous = _rail_signature_text(previous_signature)
    change = f"{previous} → {current}" if previous and previous != current else current
    kind = "직통" if getattr(item, "kind", "") == "DIRECT" else "환승"
    train_text = str(getattr(item, "train_text", "") or "") if kind == "직통" else "환승 여정"
    title = " · ".join(value for value in (kind, train_text) if value)
    return AlertEvent(
        key=key,
        alert_type=alert_type,
        transport="기차",
        notification_class="rail_direct" if kind == "직통" else "rail_transfer",
        provider="KORAIL",
        date=str(getattr(item, "date", "") or ""),
        departure_time=str(getattr(item, "departure_time", "") or ""),
        departure=str(getattr(item, "departure_station", "") or ""),
        arrival=str(getattr(item, "arrival_station", "") or ""),
        departure_code=str(getattr(item, "departure_station_code", "") or ""),
        arrival_code=str(getattr(item, "arrival_station_code", "") or ""),
        route=f"{getattr(item, 'departure_station', '')}→{getattr(item, 'arrival_station', '')}",
        title=title,
        current=current,
        change=change,
    )


def build_alert_page_url(events: Iterable[AlertEvent]) -> str:
    base_url = os.getenv("SEATWATCHER_ALERT_PAGE_URL", "").strip() or DEFAULT_ALERT_PAGE_URL
    if not base_url.startswith(("https://", "http://")):
        raise ValueError("SeatWatcher 알림 페이지 URL 설정이 필요합니다.")
    payload = [event.page_payload() for event in events]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    page_url = base_url.split('#', 1)[0]
    separator = "&" if "?" in page_url else "?"
    return f"{page_url}{separator}v={ALERT_PAGE_VERSION}#d={encoded}"


def bundle_text(events: list[AlertEvent]) -> str:
    demo = bool(events) and all(event.key.startswith("demo-") for event in events)
    heading = "🧪 SeatWatcher 테스트 알림" if demo else "🚨 SeatWatcher"
    lines = [f"{heading} | 예매 변동 {len(events)}건", ""]
    for index, event in enumerate(events[:2]):
        if index:
            lines.append("")
        item = event.kakao_item()
        lines.append(item["title"])
        lines.append(item["description"])
    lines.append("")
    if len(events) > 2:
        lines.append(f"👇 외 {len(events) - 2}건 포함 · 예매 확인에서 전체 보기")
    else:
        lines.append("👇 예매 확인에서 전체 보기")
    return "\n".join(lines)


def pushover_sound_for_event(
    event: AlertEvent,
    notification_config: dict[str, Any],
) -> str:
    pushover = notification_config.get("pushover") or {}
    if not isinstance(pushover, dict):
        raise ValueError("notification.pushover 설정 형식이 올바르지 않습니다.")
    silent_sound = str(pushover.get("silent_sound") or "").strip()
    if not silent_sound:
        raise ValueError("notification.pushover.silent_sound 설정이 필요합니다.")
    if silent_sound != "none":
        raise ValueError("notification.pushover.silent_sound는 none만 허용합니다.")
    if event.notification_class != "rail_direct":
        return silent_sound

    important_departures = {
        str(value).strip()
        for value in pushover.get("important_direct_departures", [])
        if str(value).strip()
    }
    if event.departure not in important_departures:
        return silent_sound

    important_sound = str(pushover.get("important_sound") or "").strip()
    if not important_sound:
        raise ValueError("notification.pushover.important_sound 설정이 필요합니다.")
    if important_sound not in {"vibrate", "none"}:
        raise ValueError("notification.pushover.important_sound는 vibrate 또는 none만 허용합니다.")
    return important_sound


def build_pushover_batches(
    events: list[AlertEvent],
    notification_config: dict[str, Any],
) -> list[tuple[str, list[AlertEvent]]]:
    pushover = notification_config.get("pushover") or {}
    if not isinstance(pushover, dict) or not bool(pushover.get("enabled")):
        return []
    grouped: dict[str, list[AlertEvent]] = {}
    for event in events:
        sound = pushover_sound_for_event(event, notification_config)
        grouped.setdefault(sound, []).append(event)
    return list(grouped.items())


def _send_pushover_batches(
    events: list[AlertEvent],
    *,
    page_url: str,
    notification_config: dict[str, Any],
) -> None:
    from pushover_notify import is_configured, send_message

    batches = build_pushover_batches(events, notification_config)
    if not batches:
        return
    if not is_configured():
        print("PUSHOVER_SKIPPED reason=credentials_missing")
        return
    for sound, grouped_events in batches:
        send_message(
            bundle_text(grouped_events),
            link_url=page_url,
            sound=sound,
            title="SeatWatcher 예매 변동",
        )
        print(f"PUSHOVER_SENT_BUNDLE sound={sound} count={len(grouped_events)}")


def send_alert_batch(
    events: list[AlertEvent],
    *,
    notification_config: dict[str, Any] | None = None,
) -> str:
    if not events:
        return ""
    from kakao_notify import send_to_me

    page_url = build_alert_page_url(events)
    send_to_me(bundle_text(events), link_url=page_url)
    try:
        _send_pushover_batches(
            events,
            page_url=page_url,
            notification_config=notification_config or {},
        )
    except Exception as exc:
        print(f"WARNING pushover_bundle count={len(events)}: {exc}")
    return page_url


def demo_alert_events() -> list[AlertEvent]:
    return [
        AlertEvent(
            key="demo-rail",
            alert_type="테스트",
            transport="기차",
            notification_class="rail_direct",
            provider="KORAIL",
            date="20260918",
            departure_time="180000",
            departure="서울",
            arrival="동대구",
            departure_code="0001",
            arrival_code="0015",
            route="서울→동대구",
            title="🧪 조회조건 테스트 · 실제 운행편 아님",
            current="9/18 주변 시간대 연결 확인",
            change="실제 좌석 알림 아님",
        ),
        AlertEvent(
            key="demo-bus",
            alert_type="테스트",
            transport="버스",
            notification_class="bus",
            provider="TMONEYGO",
            date="20260918",
            departure_time="180000",
            departure="성남",
            arrival="동대구",
            departure_code="",
            arrival_code="",
            route="성남→동대구",
            title="🧪 조회조건 테스트 · 실제 운행편 아님",
            current="9/18 주변 시간대 연결 확인",
            change="실제 좌석 알림 아님",
        ),
    ]
