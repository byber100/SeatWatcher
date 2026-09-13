from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

from env_loader import load_project_env

MAX_PAGES = 20


@dataclass(frozen=True)
class SearchWindow:
    departure: str
    arrival: str
    date: str
    start_time: str
    end_time: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SeatWatcher read-only one-shot probe"
    )
    parser.add_argument(
        "--provider",
        choices=("korail", "bus"),
        default="korail",
        help="조회 공급자. korail은 철도, bus는 고속/시외버스 도시 조건 조회",
    )
    parser.add_argument("--departure", default="수서")
    parser.add_argument("--arrival", default="동대구")
    parser.add_argument("--date", default="20300101", help="YYYYMMDD")
    parser.add_argument("--start", default="130000", help="HHMMSS")
    parser.add_argument("--end", default="200000", help="HHMMSS")
    parser.add_argument(
        "--direct-only",
        action="store_true",
        help="KORAIL에서 직통만 조회한다. 기본값은 직통과 환승을 각각 조회한다.",
    )
    return parser.parse_args()


def validate_window(window: SearchWindow) -> None:
    if len(window.date) != 8 or not window.date.isdigit():
        raise ValueError("--date는 YYYYMMDD 형식이어야 합니다.")
    for label, value in (("--start", window.start_time), ("--end", window.end_time)):
        if len(value) != 6 or not value.isdigit():
            raise ValueError(f"{label}는 HHMMSS 형식이어야 합니다.")
    if window.start_time > window.end_time:
        raise ValueError("--start는 --end보다 늦을 수 없습니다.")


def load_korail_credentials() -> tuple[str, str]:
    member_no = os.getenv("SEATWATCHER_KORAIL_MEMBER_NO", "").strip()
    password = os.getenv("SEATWATCHER_KORAIL_PASSWORD", "")
    if not member_no or not password:
        raise RuntimeError(
            "KORAIL 자격 증명이 없습니다. "
            "SEATWATCHER_KORAIL_MEMBER_NO와 SEATWATCHER_KORAIL_PASSWORD 환경변수를 설정하세요."
        )
    return member_no, password


def in_departure_window(departure_time: str, window: SearchWindow) -> bool:
    return window.start_time <= departure_time <= window.end_time


def availability_text(train: object) -> str:
    values = []
    for label, attr in (
        ("일반실", "general_availability_name"),
        ("특실", "special_availability_name"),
        ("입석", "standing_reservation_code"),
    ):
        value = getattr(train, attr, None)
        if value not in (None, ""):
            values.append(f"{label}={value}")

    standard = getattr(train, "standard_remaining_seat_count", None)
    first = getattr(train, "first_class_remaining_seat_count", None)
    if standard not in (None, ""):
        values.append(f"일반실잔여={standard}")
    if first not in (None, ""):
        values.append(f"특실잔여={first}")

    return ", ".join(values) if values else "좌석 상태 필드 없음"


def print_train(prefix: str, train: object) -> None:
    print(
        f"{prefix} "
        f"{getattr(train, 'train_class_name', '')} "
        f"#{getattr(train, 'train_no', '')} | "
        f"{getattr(train, 'departure_station_name', '')} "
        f"{getattr(train, 'departure_time', '')} -> "
        f"{getattr(train, 'arrival_station_name', '')} "
        f"{getattr(train, 'arrival_time', '')} | "
        f"{availability_text(train)}"
    )


def search_direct(client: object, query: object, window: SearchWindow) -> int:
    from korail_mobile_api import KorailNoDirectTrainError, KorailNoResultsError

    count = 0
    continuation = None

    for _ in range(MAX_PAGES):
        try:
            result = client.search_trains(query, continuation=continuation)
        except (KorailNoDirectTrainError, KorailNoResultsError):
            break

        page_times: list[str] = []
        for train in result.trains:
            departure_time = getattr(train, "departure_time", "")
            if departure_time:
                page_times.append(departure_time)
            if in_departure_window(departure_time, window):
                count += 1
                print_train("DIRECT", train)

        if page_times and min(page_times) > window.end_time:
            break

        continuation = result.next_page()
        if continuation is None:
            break

    return count


def search_transfer(client: object, query: object, window: SearchWindow) -> int:
    from korail_mobile_api import KorailNoResultsError

    count = 0
    continuation = None

    for _ in range(MAX_PAGES):
        try:
            result = client.search_transfer_trains(query, continuation=continuation)
        except KorailNoResultsError:
            break

        page_times: list[str] = []
        for itinerary in result.itineraries:
            first = itinerary.first
            second = itinerary.second
            departure_time = getattr(first, "departure_time", "")
            if departure_time:
                page_times.append(departure_time)
            if not in_departure_window(departure_time, window):
                continue

            count += 1
            transfer_name = getattr(itinerary, "transfer_station_name", None)
            print(
                f"TRANSFER #{count} | "
                f"환승={transfer_name or '역간이동/상이역 가능'}"
            )
            print_train("  1", first)
            print_train("  2", second)

        if page_times and min(page_times) > window.end_time:
            break

        continuation = result.next_page()
        if continuation is None:
            break

    return count


def run_korail_probe(window: SearchWindow, direct_only: bool) -> int:
    try:
        from korail_mobile_api import KorailClient, KorailConfig, TrainSearchQuery
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "korail-mobile-api가 설치되어 있지 않습니다. "
            "프로젝트 requirements.txt를 설치한 뒤 다시 실행하세요."
        ) from exc

    member_id, password = load_korail_credentials()
    client = KorailClient(KorailConfig(enable_dynapath=True))

    try:
        client.login(member_id, password)
        query = TrainSearchQuery(
            window.departure,
            window.arrival,
            window.date,
            departure_time=window.start_time,
            passengers=1,
        )

        print("[직통]")
        direct_count = search_direct(client, query, window)
        if direct_count == 0:
            print("조건에 맞는 직통 결과 없음")

        if direct_only:
            return 0

        print("\n[환승]")
        transfer_count = search_transfer(client, query, window)
        if transfer_count == 0:
            print("조건에 맞는 환승 결과 없음")

        return 0
    finally:
        try:
            client.logout()
        except Exception:
            pass
        client.close()


def run_bus_probe(window: SearchWindow) -> int:
    from bus_providers import search_user_bus_routes

    candidates = search_user_bus_routes(
        window.departure,
        window.arrival,
        window.date,
        window.start_time[:4],
        window.end_time[:4],
    )

    if not candidates:
        print("조건에 맞는 현재 예매 가능 버스 후보 없음")
        return 0

    for candidate in candidates:
        seat_text = (
            f"잔여={candidate.remaining_seats}/{candidate.total_seats}"
            if candidate.remaining_seats is not None and candidate.total_seats is not None
            else "잔여수 미확인"
        )
        print(
            f"BUS {candidate.provider} | "
            f"{candidate.departure_terminal} -> {candidate.arrival_terminal} | "
            f"{candidate.date} {candidate.departure_time[:2]}:{candidate.departure_time[2:4]} | "
            f"{candidate.company or '-'} | {candidate.bus_class or '-'} | "
            f"{candidate.schedule_type or '정규'} | {seat_text}"
        )

    return 0


def main() -> int:
    load_project_env()
    args = parse_args()
    window = SearchWindow(
        departure=args.departure,
        arrival=args.arrival,
        date=args.date,
        start_time=args.start,
        end_time=args.end,
    )
    try:
        validate_window(window)
        if args.provider == "bus":
            return run_bus_probe(window)
        return run_korail_probe(window, args.direct_only)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
