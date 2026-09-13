from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Iterable


DEFAULT_ALERT_PAGE_URL = "https://byber100.github.io/SeatWatcher/"


@dataclass(frozen=True)
class AlertEvent:
    key: str
    alert_type: str
    transport: str
    provider: str
    date: str
    departure_time: str
    route: str
    title: str
    current: str
    change: str

    def page_payload(self) -> dict[str, str]:
        data = asdict(self)
        data.pop("key", None)
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
        provider=provider,
        date=str(getattr(item, "date", "") or ""),
        departure_time=str(getattr(item, "departure_time", "") or ""),
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
        provider="KORAIL",
        date=str(getattr(item, "date", "") or ""),
        departure_time=str(getattr(item, "departure_time", "") or ""),
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
    return f"{base_url.split('#', 1)[0]}#d={encoded}"


def bundle_text(events: list[AlertEvent]) -> str:
    lines = [f"🚨 SeatWatcher | 예매 변동 {len(events)}건", ""]
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


def send_alert_batch(events: list[AlertEvent]) -> str:
    if not events:
        return ""
    from kakao_notify import send_to_me

    page_url = build_alert_page_url(events)
    send_to_me(bundle_text(events), link_url=page_url)
    return page_url


def demo_alert_events() -> list[AlertEvent]:
    return [
        AlertEvent(
            key="demo-rail",
            alert_type="좌석 변동",
            transport="기차",
            provider="KORAIL",
            date="20260923",
            departure_time="183400",
            route="서울→동대구",
            title="직통 · KTX 테스트",
            current="일반실 1석",
            change="일반실 3석 → 1석",
        ),
        AlertEvent(
            key="demo-bus",
            alert_type="예약 가능",
            transport="버스",
            provider="KOBUS",
            date="20260924",
            departure_time="191000",
            route="인천→동대구",
            title="고속버스 테스트",
            current="잔여 2석",
            change="매진 → 잔여 2석",
        ),
    ]
