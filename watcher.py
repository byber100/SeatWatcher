from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _ensure_project_python() -> None:
    """If the project .venv exists, always run SeatWatcher with that Python."""
    venv_python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not venv_python.exists():
        return
    try:
        if Path(sys.executable).resolve() == venv_python.resolve():
            return
    except OSError:
        if os.path.normcase(sys.executable) == os.path.normcase(str(venv_python)):
            return

    print(f"RUNTIME_REEXEC python={venv_python}", flush=True)
    completed = subprocess.run(
        [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]],
        cwd=str(ROOT),
        check=False,
    )
    raise SystemExit(completed.returncode)


_ensure_project_python()

from bus_providers import BusCandidate, search_user_bus_routes
from env_loader import load_project_env
from last_mile import bus_last_mile, rail_last_mile

PUBLIC_CONFIG = ROOT / "watch_targets.json"
LOCAL_CONFIG = ROOT / "watch_targets.local.json"
STATE = ROOT / ".runtime" / "watch_state.json"


def load_json(path: Path, default: dict | None = None) -> dict:
    if default is not None and not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_config() -> dict:
    path = LOCAL_CONFIG if LOCAL_CONFIG.exists() else PUBLIC_CONFIG
    return load_json(path)


def bus_key(target_id: str, item: BusCandidate) -> str:
    return "|".join((
        target_id, item.provider, item.departure_terminal, item.arrival_terminal,
        item.date, item.departure_time, item.company or "", item.bus_class or "",
        item.schedule_type or "",
    ))


def rail_key(item: object) -> str:
    return "|".join((
        item.target_id, "KORAIL", item.kind, item.date, item.departure_station,
        item.arrival_station, item.departure_time, item.arrival_time, item.train_text,
    ))


def rail_quality(item: object) -> int:
    first = int(getattr(item, "first_availability_rank", 0) or 0)
    if getattr(item, "kind", "") == "DIRECT":
        return first
    second = int(getattr(item, "second_availability_rank", 0) or 0)
    return min(first, second)


def save_state(notified: set[str], rail_quality_by_journey: dict[str, int]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(
        json.dumps(
            {
                "notified_keys": sorted(notified),
                "rail_quality_by_journey": dict(sorted(rail_quality_by_journey.items())),
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def send_message(text: str) -> None:
    from kakao_notify import send_to_me
    send_to_me(text)


def bus_message(item: BusCandidate, last_mile: str) -> str:
    seats = f"잔여 {item.remaining_seats}석" if item.remaining_seats is not None else "좌석 가능"
    schedule = f" {item.schedule_type}" if item.schedule_type else ""
    return (
        f"[SeatWatcher] {item.date[4:6]}/{item.date[6:8]} "
        f"{item.departure_time[:2]}:{item.departure_time[2:4]} "
        f"{item.departure_terminal}→{item.arrival_terminal} {item.provider}{schedule} {seats}. "
        f"{last_mile}"
    )


def rail_message(item: object, last_mile: str) -> str:
    return (
        f"[SeatWatcher] {item.date[4:6]}/{item.date[6:8]} "
        f"{item.departure_time[:2]}:{item.departure_time[2:4]} "
        f"{item.departure_station}→{item.arrival_station} {item.train_text} "
        f"{item.kind} {item.seat_text}. {last_mile}"
    )


def collect_bus(config: dict) -> list[tuple[dict, list[BusCandidate]]]:
    scans: list[tuple[dict, list[BusCandidate]]] = []
    for target in config.get("bus_targets", []):
        try:
            items = search_user_bus_routes(
                target["departure"], target["arrival"], target["date"],
                target["start"], target["end"],
                include_unavailable=True,
            )
        except Exception as exc:
            print(f"WARNING bus target={target.get('id', '?')}: {exc}")
            items = []
        scans.append((target, items))
    return scans


def collect_rail(config: dict, *, debug: bool = False) -> list[object]:
    targets = config.get("rail_targets", [])
    if not targets:
        return []
    if not os.getenv("SEATWATCHER_KORAIL_MEMBER_NO") or not os.getenv("SEATWATCHER_KORAIL_PASSWORD"):
        print("KORAIL_SKIPPED credentials_missing")
        return []
    try:
        from rail_provider import search_korail_targets
        return search_korail_targets(
            targets,
            debug=debug,
            transfer_display_direct_threshold=int(
                config.get("transfer_display_direct_threshold", 3)
            ),
        )
    except Exception as exc:
        print(f"WARNING KORAIL cycle: {exc}")
        return []


def notify_new(item_key: str, text: str, notify: bool, sent: set[str]) -> bool:
    if not notify:
        return False
    try:
        send_message(text)
    except Exception as exc:
        print(f"WARNING kakao={item_key}: {exc}")
        return False
    sent.add(item_key)
    print("KAKAO_SENT", item_key)
    return True


def run_once(config: dict, notify: bool, *, rail_debug: bool = False) -> None:
    cycle_started = time.perf_counter()
    state = load_json(STATE, {"notified_keys": [], "rail_quality_by_journey": {}})
    notified = set(state.get("notified_keys", []))
    previous_rail_quality = {
        str(key): int(value)
        for key, value in state.get("rail_quality_by_journey", {}).items()
        if str(value).isdigit()
    }
    current_rail_quality: dict[str, int] = {}
    alertable_now: set[str] = set()
    sent: set[str] = set()

    bus_scans = collect_bus(config)
    buses: list[tuple[str, BusCandidate]] = []
    bus_total = len(bus_scans)
    for index, (target, items) in enumerate(bus_scans, 1):
        target_id = target["id"]
        date = str(target["date"])
        date_text = f"{date[4:6]}/{date[6:8]}"
        start = str(target["start"])
        end = str(target["end"])
        start_text = f"{start[:2]}:{start[2:4]}"
        end_text = f"{end[:2]}:{end[2:4]}"
        print(
            f"[버스 {index:02d}/{bus_total:02d}] {date_text} "
            f"{target['departure']}->{target['arrival']} {start_text}~{end_text}"
        )

        available_count = 0
        if not items:
            print("  배차 없음")

        for item in items:
            schedule = item.schedule_type or "정규"
            company = item.company or "운수사 미상"
            bus_class = item.bus_class or "등급 미상"
            if item.bookable:
                available_count += 1
                availability = (
                    f"잔여 {item.remaining_seats}석"
                    if item.remaining_seats is not None
                    else "예약 가능"
                )
            elif item.remaining_seats == 0:
                availability = "매진 0석"
            else:
                availability = "예약 불가"

            print(
                f"  - {item.departure_time[:2]}:{item.departure_time[2:4]} | "
                f"{item.departure_terminal}->{item.arrival_terminal} | "
                f"{item.provider} | {company} | {bus_class} | {schedule} | {availability}"
            )

            if not item.bookable:
                continue

            buses.append((target_id, item))
            item_key = bus_key(target_id, item)
            safe, reason = bus_last_mile(item)
            is_new = item_key not in notified
            if not safe:
                print(f"    ! 최종교통 제외: {reason}")
                continue
            alertable_now.add(item_key)
            if is_new:
                print(f"    + NEW | {reason}")
                notify_new(item_key, bus_message(item, reason), notify, sent)

        print(f"  -> 완료 | 배차 {len(items)}편 | 현재 예약 가능 {available_count}편")

    rails = collect_rail(config, debug=rail_debug)
    rail_direct = sum(1 for item in rails if item.kind == "DIRECT")
    rail_transfer = sum(1 for item in rails if item.kind == "TRANSFER")
    print(f"기차 이용 가능 후보 | 직통 {rail_direct} | 환승 {rail_transfer}")
    for item in rails:
        item_key = rail_key(item)
        quality = rail_quality(item)
        previous_quality = previous_rail_quality.get(item_key)
        legacy_seen = item_key in notified
        is_new = previous_quality is None and not legacy_seen
        improved = previous_quality is not None and quality > previous_quality
        safe, reason = rail_last_mile(item)
        if not safe:
            marker = "BLOCKED_LASTMILE"
        elif improved:
            marker = f"UPGRADED {previous_quality}->{quality}"
        elif is_new:
            marker = "NEW"
        else:
            marker = "ACTIVE"

        date_text = f"{item.date[4:6]}/{item.date[6:8]}"
        dep_text = f"{item.departure_time[:2]}:{item.departure_time[2:4]}"
        arr_text = f"{item.arrival_time[:2]}:{item.arrival_time[2:4]}"
        kind_text = "직통" if item.kind == "DIRECT" else "환승"
        print(f"[{marker} 기차 {kind_text}] {date_text} {item.departure_station}->{item.arrival_station}")
        if item.kind == "DIRECT":
            print(f"  {dep_text}->{arr_text} | {item.train_text}")
        else:
            for part in item.train_text.split(" / "):
                print(f"  {part}")
        print(f"  이용형태: {item.seat_text}")
        print(f"  연결: {reason}")
        if not safe:
            continue

        alertable_now.add(item_key)
        should_alert = is_new or improved
        if should_alert:
            if improved:
                print(f"    + UPGRADED | 이용품질 {previous_quality}->{quality}")
            delivered = notify_new(item_key, rail_message(item, reason), notify, sent)
            if notify and not delivered:
                if previous_quality is not None:
                    current_rail_quality[item_key] = previous_quality
                continue
        current_rail_quality[item_key] = quality

    if notify:
        save_state((notified & alertable_now) | sent, current_rail_quality)
    elapsed = time.perf_counter() - cycle_started
    print(
        f"SUMMARY bus={len(buses)} rail={len(rails)} alertable={len(alertable_now)} "
        f"notify={notify} cycle_seconds={elapsed:.1f}"
    )


def main() -> int:
    load_project_env()
    parser = argparse.ArgumentParser(description="SeatWatcher recurring watcher")
    parser.add_argument("--watch", action="store_true", help="설정된 주기로 계속 감시")
    parser.add_argument("--notify", action="store_true", help="새 후보를 카카오로 알림")
    parser.add_argument("--rail-debug", action="store_true", help="KORAIL 상세 진단 로그 표시")
    args = parser.parse_args()
    config = load_config()
    interval = max(60, int(config.get("poll_interval_seconds", 180)))
    while True:
        try:
            run_once(config, args.notify, rail_debug=args.rail_debug)
        except Exception as exc:
            print(f"ERROR cycle: {exc}")
        if not args.watch:
            return 0
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("SeatWatcher stopped")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
