from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

SEAT_RESERVABLE_CODE = "11"
SOLD_OUT_CODE = "13"
STANDBY_WAIT_FLAG = " 9"
ALL_TRAIN_GROUP_CODE = "109"
DEFAULT_TRANSFER_DISPLAY_DIRECT_THRESHOLD = 3


@dataclass(frozen=True)
class RailCandidate:
    target_id: str
    kind: str
    date: str
    departure_station: str
    arrival_station: str
    departure_time: str
    arrival_time: str
    train_text: str
    seat_text: str
    first_availability_rank: int = 0
    second_availability_rank: int = 0


@dataclass
class ModeScanResult:
    candidates: list[RailCandidate] = field(default_factory=list)
    alternate_lines: list[str] = field(default_factory=list)
    scheduled_count: int = 0
    train_types: set[str] = field(default_factory=set)
    seated_count: int = 0
    standing_count: int = 0
    mixed_count: int = 0
    filtered_transfer_count: int = 0
    dominated_transfer_count: int = 0
    waitlist_count: int = 0
    schedule_lines: list[str] = field(default_factory=list)
    transfer_station_counts: dict[str, int] = field(default_factory=dict)


def _int(value: object) -> int | None:
    try:
        return int(str(value).strip()) if value not in (None, "") else None
    except ValueError:
        return None


def _time_text(hhmmss: object) -> str:
    digits = "".join(ch for ch in str(hhmmss or "") if ch.isdigit())
    if len(digits) < 4:
        return str(hhmmss or "?")
    return f"{digits[:2]}:{digits[2:4]}"


def _clock_minutes(hhmmss: object) -> int | None:
    digits = "".join(ch for ch in str(hhmmss or "") if ch.isdigit())
    if len(digits) < 4:
        return None
    hour = int(digits[:2])
    minute = int(digits[2:4])
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _transfer_wait_text(first_arrival: object, second_departure: object) -> str:
    arrival = _clock_minutes(first_arrival)
    departure = _clock_minutes(second_departure)
    if arrival is None or departure is None:
        return "대기시간 미상"
    wait = departure - arrival
    if wait < 0:
        wait += 24 * 60
    return f"대기 {wait}분"


def _train_type(train: object) -> str:
    return str(
        getattr(train, "train_class_name", "")
        or getattr(train, "train_group_name", "")
        or "기타"
    ).strip()


def _name(train: object) -> str:
    return f"{_train_type(train)} {getattr(train, 'train_no', '')}".strip()


def _station_name(
    train: object,
    name_attr: str,
    code_attr: str,
    station_names: dict[str, str],
    fallback: str = "?",
) -> str:
    name = str(getattr(train, name_attr, "") or "").strip()
    if name:
        return name
    code = str(getattr(train, code_attr, "") or "").strip()
    return station_names.get(code, fallback)


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(value))


def _reasonable_transfer(
    departure_station: str,
    transfer_station: str,
    arrival_station: str,
    station_locations: dict[str, tuple[float, float]],
) -> tuple[bool, str]:
    departure = station_locations.get(departure_station)
    transfer = station_locations.get(transfer_station)
    arrival = station_locations.get(arrival_station)
    if departure is None or transfer is None or arrival is None:
        return False, "역 좌표 미확인"

    direct_km = _haversine_km(departure, arrival)
    via_km = _haversine_km(departure, transfer) + _haversine_km(transfer, arrival)
    allowed_extra_km = max(25.0, direct_km * 0.15)
    extra_km = via_km - direct_km
    return extra_km <= allowed_extra_km, f"우회 +{extra_km:.1f}km"


def _seat_options(train: object) -> tuple[list[str], set[str]]:
    """Return distinct seated / standing / mixed availability from KORAIL codes."""
    labels: list[str] = []
    categories: set[str] = set()

    general_code = str(getattr(train, "general_reservation_code", "") or "").strip()
    special_code = str(getattr(train, "special_reservation_code", "") or "").strip()
    standing_code = str(getattr(train, "standing_reservation_code", "") or "").strip()
    free_code = str(getattr(train, "free_reservation_code", "") or "").strip()
    merge_flag = str(getattr(train, "merge_seat_application_flag", "") or "").strip()

    general_count = _int(getattr(train, "standard_remaining_seat_count", None))
    special_count = _int(getattr(train, "first_class_remaining_seat_count", None))

    general_seated = general_code == SEAT_RESERVABLE_CODE or (general_count or 0) > 0
    special_seated = special_code == SEAT_RESERVABLE_CODE or (special_count or 0) > 0

    if general_seated:
        label = "일반실 좌석 가능"
        if general_count is not None and general_count > 0:
            label += f"({general_count})"
        labels.append(label)
        categories.add("seated")

    if special_seated:
        label = "특실 좌석 가능"
        if special_count is not None and special_count > 0:
            label += f"({special_count})"
        labels.append(label)
        categories.add("seated")

    if general_code == SOLD_OUT_CODE and standing_code == SEAT_RESERVABLE_CODE:
        labels.append("입석 가능")
        categories.add("standing")
    if free_code == SEAT_RESERVABLE_CODE:
        labels.append("자유석 가능")
        categories.add("free")

    if not general_seated and merge_flag in {"A", "G"}:
        labels.append("일반실 입석+좌석 가능")
        categories.add("mixed")
    if not special_seated and merge_flag in {"A", "S"}:
        labels.append("특실 입석+좌석 가능")
        categories.add("mixed")

    return labels, categories


def _seat_text(train: object) -> str:
    labels, _ = _seat_options(train)
    return ", ".join(labels) if labels else "이용 가능 좌석/입석 없음"


def _waitlist_available(train: object) -> bool:
    return str(getattr(train, "wait_reservation_flag", "") or "") == STANDBY_WAIT_FLAG


def _availability_summary(train: object) -> str:
    labels, _ = _seat_options(train)
    if labels:
        return ", ".join(labels)

    general_code = str(getattr(train, "general_reservation_code", "") or "").strip()
    special_code = str(getattr(train, "special_reservation_code", "") or "").strip()
    states: list[str] = []
    if general_code == SOLD_OUT_CODE:
        states.append("일반실 매진")
    elif general_code and general_code != SEAT_RESERVABLE_CODE:
        states.append("일반실 예약불가")
    if special_code == SOLD_OUT_CODE:
        states.append("특실 매진")
    elif special_code not in ("", "00", SEAT_RESERVABLE_CODE):
        states.append("특실 예약불가")
    if _waitlist_available(train):
        states.append("예약대기 가능")
    return ", ".join(states) if states else "예약 가능 좌석 없음"


def _availability_rank(train: object) -> int:
    """예약 지정좌석 > 자유석 > 입석/입석+좌석 > 이용불가."""
    _, categories = _seat_options(train)
    if "seated" in categories:
        return 3
    if "free" in categories:
        return 2
    if "standing" in categories or "mixed" in categories:
        return 1
    return 0


def _journey_minutes(candidate: RailCandidate) -> int | None:
    departure = _clock_minutes(candidate.departure_time)
    arrival = _clock_minutes(candidate.arrival_time)
    if departure is None or arrival is None:
        return None
    if arrival < departure:
        arrival += 24 * 60
    return arrival - departure


def _transfer_availability_key(candidate: RailCandidate) -> tuple[int, int]:
    """환승 전체 품질은 더 나쁜 구간을 먼저 비교한다."""
    ranks = sorted((candidate.first_availability_rank, candidate.second_availability_rank))
    return ranks[0], ranks[1]


def _candidate_availability_rank(candidate: RailCandidate) -> int:
    if candidate.kind == "DIRECT":
        return candidate.first_availability_rank
    return min(candidate.first_availability_rank, candidate.second_availability_rank)


def _transfer_dominates(a: RailCandidate, b: RailCandidate) -> bool:
    if a.kind != "TRANSFER" or b.kind != "TRANSFER":
        return False
    # 서로 다른 출발시각은 사용자가 선택할 수 있는 별도 옵션으로 보존한다.
    if _clock_minutes(a.departure_time) != _clock_minutes(b.departure_time):
        return False

    duration_a = _journey_minutes(a)
    duration_b = _journey_minutes(b)
    if duration_a is None or duration_b is None:
        return False

    # 1순위: 총 소요시간. 열차 기종이나 환승 대기시간은 우열 점수에 넣지 않는다.
    if duration_a < duration_b:
        return True
    if duration_a > duration_b:
        return False

    # 2순위: 지정좌석 예약 > 자유석 > 입석/입석+좌석.
    return _transfer_availability_key(a) > _transfer_availability_key(b)


def _filter_dominated_transfers(candidates: list[RailCandidate]) -> tuple[list[RailCandidate], int]:
    kept: list[RailCandidate] = []
    dominated = 0
    for index, candidate in enumerate(candidates):
        if any(
            other_index != index and _transfer_dominates(other, candidate)
            for other_index, other in enumerate(candidates)
        ):
            dominated += 1
            continue
        kept.append(candidate)
    return kept, dominated


def _continuation_key(continuation: object) -> tuple[str, str, str, str]:
    return (
        str(getattr(continuation, "query_station_no", "") or ""),
        str(getattr(continuation, "query_train_no", "") or ""),
        str(getattr(continuation, "query_train_no2", "") or ""),
        str(getattr(continuation, "page_count", "") or ""),
    )


def _plus_one_second(hhmmss: str) -> str | None:
    digits = "".join(ch for ch in str(hhmmss or "") if ch.isdigit()).zfill(6)
    if len(digits) != 6:
        return None
    hour = int(digits[:2])
    minute = int(digits[2:4])
    second = int(digits[4:6])
    total = hour * 3600 + minute * 60 + second + 1
    if total >= 24 * 3600:
        return None
    return f"{total // 3600:02d}{(total % 3600) // 60:02d}{total % 60:02d}"


def _fallback_query_start(current_start: str, page_departures: list[str], result: object) -> str | None:
    """Continue by departure time when KORAIL says more pages exist but omits cursor fields."""
    metadata = getattr(result, "metadata", None)
    if getattr(metadata, "next_page_flag", None) != "Y" or not page_departures:
        return None
    last_departure = max(page_departures)
    if last_departure > current_start:
        return last_departure
    return _plus_one_second(last_departure)


def _record_categories(result: ModeScanResult, categories: set[str]) -> None:
    if "seated" in categories:
        result.seated_count += 1
    if "standing" in categories:
        result.standing_count += 1
    if "mixed" in categories:
        result.mixed_count += 1


def _direct(
    client,
    api,
    target: dict,
    station_names: dict[str, str],
) -> ModeScanResult:
    scan = ModeScanResult()
    continuation = None
    query_start = target["start"]
    seen_continuations: set[tuple[str, str, str, str]] = set()
    seen_rows: set[tuple[str, str, str, str]] = set()

    while True:
        query = api.TrainSearchQuery(
            target["departure"],
            target["arrival"],
            target["date"],
            departure_time=query_start,
            passengers=1,
            train_group_code=ALL_TRAIN_GROUP_CODE,
            include_srt=True,
        )
        try:
            result = client.search_trains(query, continuation=continuation)
        except (api.KorailNoDirectTrainError, api.KorailNoResultsError):
            break

        page_departures: list[str] = []
        for train in result.trains:
            dep = str(getattr(train, "departure_time", "") or "").zfill(6)
            arrival_time = str(getattr(train, "arrival_time", "") or "").zfill(6)
            if dep:
                page_departures.append(dep)
            if not (target["start"] <= dep <= target["end"]):
                continue

            row_key = (
                str(getattr(train, "train_no", "") or ""),
                dep,
                arrival_time,
                _train_type(train),
            )
            if row_key in seen_rows:
                continue
            seen_rows.add(row_key)

            scan.scheduled_count += 1
            scan.train_types.add(_train_type(train))
            departure_station = _station_name(
                train,
                "departure_station_name",
                "departure_station_code",
                station_names,
                target["departure"],
            )
            arrival_station = _station_name(
                train,
                "arrival_station_name",
                "arrival_station_code",
                station_names,
                target["arrival"],
            )
            scan.schedule_lines.append(
                f"{_time_text(dep)}→{_time_text(arrival_time)} | {_name(train)} | "
                f"{_availability_summary(train)}"
            )

            labels, categories = _seat_options(train)
            _record_categories(scan, categories)
            waitlist = _waitlist_available(train)
            if waitlist and not labels:
                scan.waitlist_count += 1
            if not labels and not waitlist:
                continue
            scan.candidates.append(
                RailCandidate(
                    target["id"],
                    "DIRECT",
                    target["date"],
                    departure_station,
                    arrival_station,
                    dep,
                    arrival_time,
                    _name(train),
                    ", ".join(labels) if labels else "예약대기 가능",
                    _availability_rank(train) if labels else 0,
                    0,
                )
            )

        if page_departures and min(page_departures) > target["end"]:
            break

        next_continuation = result.next_page()
        if next_continuation is not None:
            key = _continuation_key(next_continuation)
            if key in seen_continuations:
                print(f"WARNING KORAIL pagination repeated target={target.get('id', '?')} mode=DIRECT")
                break
            seen_continuations.add(key)
            continuation = next_continuation
            continue

        fallback_start = _fallback_query_start(query_start, page_departures, result)
        if fallback_start is None or fallback_start > target["end"]:
            break
        query_start = fallback_start
        continuation = None

    return scan


def _transfer(
    client,
    api,
    target: dict,
    station_names: dict[str, str],
    station_locations: dict[str, tuple[float, float]],
) -> ModeScanResult:
    scan = ModeScanResult()
    continuation = None
    query_start = target["start"]
    seen_continuations: set[tuple[str, str, str, str]] = set()
    seen_rows: set[tuple[str, str, str, str, str, str]] = set()

    while True:
        query = api.TrainSearchQuery(
            target["departure"],
            target["arrival"],
            target["date"],
            departure_time=query_start,
            passengers=1,
            train_group_code=ALL_TRAIN_GROUP_CODE,
            include_srt=True,
        )
        try:
            result = client.search_transfer_trains(query, continuation=continuation)
        except api.KorailNoResultsError:
            break

        page_departures: list[str] = []
        for itinerary in result.itineraries:
            first, second = itinerary.first, itinerary.second
            dep = str(getattr(first, "departure_time", "") or "").zfill(6)
            first_arrival = str(getattr(first, "arrival_time", "") or "").zfill(6)
            second_departure = str(getattr(second, "departure_time", "") or "").zfill(6)
            second_arrival = str(getattr(second, "arrival_time", "") or "").zfill(6)
            if dep:
                page_departures.append(dep)
            if not (target["start"] <= dep <= target["end"]):
                continue

            row_key = (
                str(getattr(first, "train_no", "") or ""),
                dep,
                first_arrival,
                str(getattr(second, "train_no", "") or ""),
                second_departure,
                second_arrival,
            )
            if row_key in seen_rows:
                continue
            seen_rows.add(row_key)

            scan.scheduled_count += 1
            scan.train_types.add(_train_type(first))
            scan.train_types.add(_train_type(second))

            departure_station = _station_name(
                first,
                "departure_station_name",
                "departure_station_code",
                station_names,
                target["departure"],
            )
            transfer_station = _station_name(
                first,
                "arrival_station_name",
                "arrival_station_code",
                station_names,
                "",
            ) or _station_name(
                second,
                "departure_station_name",
                "departure_station_code",
                station_names,
                "환승역 미상",
            )
            arrival_station = _station_name(
                second,
                "arrival_station_name",
                "arrival_station_code",
                station_names,
                target["arrival"],
            )

            reasonable, _ = _reasonable_transfer(
                departure_station,
                transfer_station,
                arrival_station,
                station_locations,
            )
            if not reasonable:
                scan.filtered_transfer_count += 1
                continue

            scan.transfer_station_counts[transfer_station] = (
                scan.transfer_station_counts.get(transfer_station, 0) + 1
            )

            first_labels, first_categories = _seat_options(first)
            second_labels, second_categories = _seat_options(second)
            combined_categories = first_categories | second_categories
            _record_categories(scan, combined_categories)

            wait_text = _transfer_wait_text(first_arrival, second_departure)
            route_text = (
                f"1구간 {_name(first)} | {_time_text(dep)} {departure_station} 탑승 → "
                f"{_time_text(first_arrival)} {transfer_station} 하차 | "
                f"환승 {transfer_station} ({wait_text}) | "
                f"2구간 {_name(second)} | {_time_text(second_departure)} {transfer_station} 탑승 → "
                f"{_time_text(second_arrival)} {arrival_station} 도착"
            )
            first_state = ", ".join(first_labels) if first_labels else _availability_summary(first)
            second_state = ", ".join(second_labels) if second_labels else _availability_summary(second)
            availability_text = f"1구간:{first_state} / 2구간:{second_state}"
            scan.schedule_lines.append(
                f"{_time_text(dep)}→{_time_text(second_arrival)} | "
                f"{route_text} | {availability_text}"
            )

            if not first_labels or not second_labels:
                continue
            scan.candidates.append(
                RailCandidate(
                    target["id"],
                    "TRANSFER",
                    target["date"],
                    departure_station,
                    arrival_station,
                    dep,
                    second_arrival,
                    route_text,
                    availability_text,
                    _availability_rank(first),
                    _availability_rank(second),
                )
            )

        if page_departures and min(page_departures) > target["end"]:
            break

        next_continuation = result.next_page()
        if next_continuation is not None:
            key = _continuation_key(next_continuation)
            if key in seen_continuations:
                print(f"WARNING KORAIL pagination repeated target={target.get('id', '?')} mode=TRANSFER")
                break
            seen_continuations.add(key)
            continuation = next_continuation
            continue

        fallback_start = _fallback_query_start(query_start, page_departures, result)
        if fallback_start is None or fallback_start > target["end"]:
            break
        query_start = fallback_start
        continuation = None

    return scan


def _search_with_retry(
    client,
    api,
    target: dict,
    mode: str,
    station_names: dict[str, str],
    station_locations: dict[str, tuple[float, float]],
) -> ModeScanResult:
    """Search one direct/transfer mode, retrying one transient transport failure."""
    for attempt in range(2):
        try:
            if mode == "DIRECT":
                return _direct(client, api, target, station_names)
            return _transfer(client, api, target, station_names, station_locations)
        except api.KorailTransportError:
            if attempt == 0:
                print(f"KORAIL_RETRY target={target.get('id', '?')} mode={mode} reason=transport")
                continue
            raise
    return ModeScanResult()


def _types_text(*scans: ModeScanResult) -> str:
    values: set[str] = set()
    for scan in scans:
        values.update(name for name in scan.train_types if name)
    return ",".join(sorted(values)) if values else "-"


def search_korail_targets(
    targets: list[dict],
    *,
    debug: bool = False,
    transfer_display_direct_threshold: int = DEFAULT_TRANSFER_DISPLAY_DIRECT_THRESHOLD,
) -> list[RailCandidate]:
    member_no = os.getenv("SEATWATCHER_KORAIL_MEMBER_NO", "").strip()
    password = os.getenv("SEATWATCHER_KORAIL_PASSWORD", "")
    if not member_no or not password:
        raise RuntimeError("KORAIL 환경변수 자격 증명이 없습니다.")
    try:
        import korail_mobile_api as api
    except ModuleNotFoundError as exc:
        raise RuntimeError("SeatWatcher .venv Python을 사용하세요.") from exc

    client = api.KorailClient(api.KorailConfig(enable_dynapath=True))
    rows: list[RailCandidate] = []
    errors: list[str] = []
    total_scheduled_direct = 0
    total_scheduled_transfer = 0
    total_filtered_transfer = 0
    total_dominated_transfer = 0
    try:
        client.login(member_no, password)
        station_rows = client.get_station_data().stations
        station_names = {
            str(station.code): str(station.name)
            for station in station_rows
            if station.code and station.name
        }
        station_locations: dict[str, tuple[float, float]] = {}
        for station in station_rows:
            if not station.name or station.latitude in (None, "") or station.longitude in (None, ""):
                continue
            try:
                station_locations[str(station.name)] = (
                    float(station.latitude),
                    float(station.longitude),
                )
            except (TypeError, ValueError):
                continue
        total = len(targets)
        print(f"KORAIL 시작 | 검색조건 {total}개 | 전체열차(KTX·ITX·무궁화·새마을·기타)")
        for index, target in enumerate(targets, 1):
            target_id = target.get("id", "?")
            route = f"{target.get('departure', '?')}->{target.get('arrival', '?')}"
            date = str(target.get("date", "?"))
            start = str(target.get("start", "?"))
            end = str(target.get("end", "?"))
            date_text = f"{date[4:6]}/{date[6:8]}" if len(date) >= 8 else date
            start_text = f"{start[:2]}:{start[2:4]}" if len(start) >= 4 else start
            end_text = f"{end[:2]}:{end[2:4]}" if len(end) >= 4 else end
            print(
                f"[기차 {index:02d}/{total:02d}] {date_text} {route} "
                f"{start_text}~{end_text} 검색 중..."
            )

            direct_scan = ModeScanResult()
            transfer_scan = ModeScanResult()
            target_failed = False

            for mode in ("DIRECT", "TRANSFER"):
                mode_text = "직통" if mode == "DIRECT" else "환승"
                print(f"  {mode_text} 검색 중...")
                try:
                    found = _search_with_retry(
                        client,
                        api,
                        target,
                        mode,
                        station_names,
                        station_locations,
                    )
                except Exception as exc:
                    target_failed = True
                    error_id = f"{target_id}:{mode}"
                    errors.append(error_id)
                    print(f"WARNING KORAIL target={target_id} mode={mode}: {exc}")
                    continue
                if mode == "DIRECT":
                    direct_scan = found
                else:
                    transfer_scan = found

            transfer_scan.candidates, transfer_scan.dominated_transfer_count = (
                _filter_dominated_transfers(transfer_scan.candidates)
            )

            total_scheduled_direct += direct_scan.scheduled_count
            total_scheduled_transfer += transfer_scan.scheduled_count
            total_filtered_transfer += transfer_scan.filtered_transfer_count
            total_dominated_transfer += transfer_scan.dominated_transfer_count
            usable_transfer = transfer_scan.scheduled_count - transfer_scan.filtered_transfer_count
            direct_usable_count = sum(
                _candidate_availability_rank(item) > 0 for item in direct_scan.candidates
            )
            threshold = max(0, int(transfer_display_direct_threshold))
            promote_transfer = threshold <= 0 or direct_usable_count < threshold
            promoted_transfer_candidates = (
                transfer_scan.candidates if promote_transfer else []
            )
            rows.extend(direct_scan.candidates)
            rows.extend(promoted_transfer_candidates)
            target_candidates = direct_scan.candidates + promoted_transfer_candidates
            reserved_count = sum(_candidate_availability_rank(item) == 3 for item in target_candidates)
            free_count = sum(_candidate_availability_rank(item) == 2 for item in target_candidates)
            standing_count = sum(_candidate_availability_rank(item) == 1 for item in target_candidates)
            waitlist_count = direct_scan.waitlist_count
            status_text = "일부실패" if target_failed else "완료"

            print("  직통 운행")
            if direct_scan.schedule_lines:
                for line in direct_scan.schedule_lines:
                    print(f"    - {line}")
            else:
                print("    - 해당 시간대 직통 없음")

            print("  환승 운행")
            if transfer_scan.schedule_lines:
                for line in transfer_scan.schedule_lines:
                    print(f"    - {line}")
            else:
                print("    - 해당 시간대 실사용 환승 없음")

            if transfer_scan.transfer_station_counts:
                station_text = " / ".join(
                    f"{station} {count}개"
                    for station, count in sorted(
                        transfer_scan.transfer_station_counts.items(),
                        key=lambda item: (-item[1], item[0]),
                    )
                )
                print(f"  환승 검색 요약 | {usable_transfer}개 | {station_text}")
            else:
                print("  환승 검색 요약 | 실사용 환승 없음")

            if transfer_scan.candidates:
                if promote_transfer:
                    print(
                        f"  환승 후보 승격 | 직통 이용가능 {direct_usable_count}건 "
                        f"< 기준 {threshold}건 | 현재 이용가능 환승 {len(promoted_transfer_candidates)}건"
                    )
                else:
                    print(
                        f"  환승 후보 대기 | 직통 이용가능 {direct_usable_count}건 "
                        f">= 기준 {threshold}건 | 현재 이용가능 환승 {len(transfer_scan.candidates)}건"
                    )

            print(
                f"  -> {status_text} | 감시 후보 {len(target_candidates)}건 "
                f"(지정좌석 {reserved_count} / 자유석 {free_count} / "
                f"입석·혼합 {standing_count} / 예약대기 {waitlist_count})"
            )
            if debug:
                print(
                    f"     DEBUG target={target_id} returned_transfer={transfer_scan.scheduled_count} "
                    f"filtered_transfer={transfer_scan.filtered_transfer_count} "
                    f"dominated_transfer={transfer_scan.dominated_transfer_count} "
                    f"seat_signals={direct_scan.seated_count + transfer_scan.seated_count} "
                    f"standing_signals={direct_scan.standing_count + transfer_scan.standing_count} "
                    f"mixed_signals={direct_scan.mixed_count + transfer_scan.mixed_count} "
                    f"waitlist={waitlist_count} "
                    f"types={_types_text(direct_scan, transfer_scan)}"
                )
                for line in direct_scan.alternate_lines:
                    print(f"     ALT {line}")
                for line in transfer_scan.alternate_lines:
                    print(f"     ALT {line}")

        direct_available_count = sum(
            1 for item in rows
            if item.kind == "DIRECT" and _candidate_availability_rank(item) > 0
        )
        direct_waitlist_count = sum(
            1 for item in rows
            if item.kind == "DIRECT" and _candidate_availability_rank(item) == 0
        )
        transfer_count = sum(1 for item in rows if item.kind == "TRANSFER")
        ok_count = total - len({entry.split(':', 1)[0] for entry in errors})
        print(
            f"KORAIL 완료 | {ok_count}/{total} 조건 정상 | "
            f"직통 이용가능 {direct_available_count} / 직통 예약대기 {direct_waitlist_count} / "
            f"환승 알림후보 {transfer_count} | 오류 {total - ok_count}"
        )
        if debug:
            print(
                f"KORAIL DEBUG | scheduled_direct={total_scheduled_direct} "
                f"returned_transfer={total_scheduled_transfer} "
                f"usable_transfer={total_scheduled_transfer - total_filtered_transfer} "
                f"filtered_transfer={total_filtered_transfer} "
                f"dominated_transfer={total_dominated_transfer}"
            )
        if errors:
            print(f"KORAIL_PARTIAL mode_errors={len(errors)} ids={','.join(errors)}")
    finally:
        try:
            client.logout()
        except Exception:
            pass
        client.close()
    return sorted(rows, key=lambda item: (item.date, item.departure_time, item.target_id, item.kind))
