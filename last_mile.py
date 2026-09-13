from __future__ import annotations

BUS_MINUTES = {
    ("KOBUS", "서울경부", "동대구"): 210,
    ("KOBUS", "동서울", "동대구"): 210,
    ("KOBUS", "인천", "동대구"): 220,
    ("KOBUS", "동대구", "서울경부"): 210,
    ("KOBUS", "동대구", "동서울"): 210,
    ("KOBUS", "동대구", "인천"): 220,
    # 성남->동대구는 현재 노선 안내의 3시간 20~30분 범위 중 긴 값으로 보수 적용한다.
    # 다른 티머니/버스타고 노선에는 이 값을 자동 확장하지 않는다.
    ("TMONEY_INTERCITY", "성남", "동대구"): 210,
    ("BUSTAGO", "성남종합", "동대구"): 210,
}

# 동대구역 도착 후 1호선->반월당->2호선 신매 환승을 위한 보수적 마감.
DAEGU_ARRIVAL_LIMIT = 23 * 60 + 5
# 인천종합터미널 도착 후 인천1호선 인천터미널->인천권 환승역 이동 마감.
INCHEON_BUS_ARRIVAL_LIMIT = 24 * 60 + 35
# 서울/수원/광명 철도역 도착 뒤 수도권 최종 목적지까지의 보수적 연결 마감.
# 인천권 철도는 실제 KORAIL 장거리 허브인 광명역을 사용한다.
CAPITAL_RAIL_LIMITS = {"서울": 22 * 60 + 30, "수원": 22 * 60 + 15, "광명": 22 * 60 + 30}
SEOUL_BUS_ARRIVAL_LIMIT = 22 * 60 + 30


def _minutes(hhmm: str) -> int | None:
    digits = "".join(ch for ch in str(hhmm) if ch.isdigit())
    if len(digits) < 4:
        return None
    hour, minute = int(digits[:2]), int(digits[2:4])
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _arrival_minutes(departure_hhmm: str, arrival_hhmm: str) -> int | None:
    departure = _minutes(departure_hhmm)
    arrival = _minutes(arrival_hhmm)
    if departure is None or arrival is None:
        return None
    if arrival < departure:
        arrival += 24 * 60
    return arrival


def _shown_time(total_minutes: int) -> str:
    day = total_minutes // (24 * 60)
    clock = total_minutes % (24 * 60)
    prefix = f"다음날+{day} " if day > 1 else ("다음날 " if day == 1 else "")
    return f"{prefix}{clock // 60:02d}:{clock % 60:02d}"


def bus_last_mile(item: object) -> tuple[bool, str]:
    provider = str(getattr(item, "provider", ""))
    departure_terminal = str(getattr(item, "departure_terminal", ""))
    arrival_terminal = str(getattr(item, "arrival_terminal", ""))
    duration = BUS_MINUTES.get((provider, departure_terminal, arrival_terminal))
    departure = _minutes(getattr(item, "departure_time", ""))
    if duration is None or departure is None:
        return False, "소요시간 미검증"

    arrival = departure + duration
    shown = _shown_time(arrival)
    source_text = "예상" if provider == "KOBUS" else "보수 예상"

    if arrival_terminal == "동대구":
        return arrival <= DAEGU_ARRIVAL_LIMIT, f"동대구 {source_text} {shown}"
    if arrival_terminal == "인천":
        return arrival <= INCHEON_BUS_ARRIVAL_LIMIT, f"인천 {source_text} {shown}"
    if arrival_terminal in ("서울경부", "동서울"):
        return arrival <= SEOUL_BUS_ARRIVAL_LIMIT, f"서울 {source_text} {shown}"
    return False, "최종 목적지 연결 미검증"


def rail_last_mile(item: object) -> tuple[bool, str]:
    station = str(getattr(item, "arrival_station", ""))
    if station == "경산":
        # 경산 후보는 target에 arrival=경산이 명시된 경우에만 생성하는 것이 상위 계약이다.
        return True, "명시적 경산역 도착 후 숙박 가능"

    arrival = _arrival_minutes(
        getattr(item, "departure_time", ""),
        getattr(item, "arrival_time", ""),
    )
    if arrival is None:
        return False, "철도 도착시각 미확인"

    shown = _shown_time(arrival)
    if station == "동대구":
        return arrival <= DAEGU_ARRIVAL_LIMIT, f"동대구 {shown}"
    if station in CAPITAL_RAIL_LIMITS:
        reason = f"{station} {shown}"
        if station == "광명":
            reason += " -> 인천권 최종 이동"
        return arrival <= CAPITAL_RAIL_LIMITS[station], reason
    return False, "최종 목적지 연결 미검증"
