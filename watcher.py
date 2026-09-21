from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
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

from alert_bundle import (
    AlertEvent,
    build_alert_page_url,
    build_pushover_alert_page_url,
    build_bus_event,
    build_rail_event,
    demo_alert_events,
    send_alert_batch,
)
from bus_providers import BusCandidate, search_user_bus_routes
from env_loader import load_project_env
from last_mile import bus_last_mile, rail_last_mile
from standby_reservation import attempt_auto_waitlist, retry_pending_waitlist_notifications

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


def expand_rail_targets_for_wide_test(config: dict) -> dict:
    """Add a temporary high-turnover KORAIL test scope without modifying the saved config."""
    original_targets = list(config.get("rail_targets", []))
    if not original_targets:
        print("RAIL_TEST_SCOPE skipped=no_rail_targets")
        return config

    preferred_pair = ("서울", "동대구")
    pairs = [preferred_pair, (preferred_pair[1], preferred_pair[0])]

    dates = ["20260918"]

    extras: list[dict] = []
    for pair_index, (departure, arrival) in enumerate(pairs, start=1):
        for date_text in dates:
            extras.append(
                {
                    "id": f"rail-wide-test-{date_text}-{pair_index}",
                    "date": date_text,
                    "departure": departure,
                    "arrival": arrival,
                    "start": "050000",
                    "end": "235900",
                    "suppress_initial_alert": True,
                }
            )

    expanded = dict(config)
    expanded["rail_targets"] = original_targets + extras
    print(
        "RAIL_TEST_SCOPE "
        f"expanded={len(extras)} dates={','.join(dates)} "
        "routes=서울-동대구,동대구-서울 window=050000-235900 soldout=separate"
    )
    return expanded


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


UNAVAILABLE_SIGNATURE = "unavailable"


def target_query_key(target: dict) -> tuple[str, ...]:
    return tuple(
        str(target.get(name, ""))
        for name in (
            "departure",
            "arrival",
            "date",
            "start",
            "end",
            "direct_only",
            "train_type_prefix",
            "arrival_before",
        )
    )


def state_target_id(item_key: str) -> str:
    return item_key.split("|", 1)[0]


def is_rail_state_key(item_key: str) -> bool:
    parts = item_key.split("|", 2)
    return len(parts) >= 2 and parts[1] == "KORAIL"


def bus_availability_signature(item: BusCandidate) -> str:
    if not item.bookable:
        return UNAVAILABLE_SIGNATURE
    remaining = "?" if item.remaining_seats is None else str(item.remaining_seats)
    return f"bookable|remaining={remaining}"


def rail_availability_signature(item: object) -> str:
    quality = rail_quality(item)
    prefix = "bookable" if quality > 0 else "waitlist"
    return f"{prefix}|quality={quality}|{getattr(item, 'seat_text', '')}"


def save_state(
    notified: set[str],
    bus_availability_by_journey: dict[str, str],
    rail_availability_by_journey: dict[str, str],
    rail_quality_by_journey: dict[str, int],
    baselined_target_ids: set[str] | None = None,
) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(
        json.dumps(
            {
                "notified_keys": sorted(notified),
                "bus_availability_by_journey": dict(
                    sorted(bus_availability_by_journey.items())
                ),
                "rail_availability_by_journey": dict(
                    sorted(rail_availability_by_journey.items())
                ),
                "rail_quality_by_journey": dict(sorted(rail_quality_by_journey.items())),
                "baselined_target_ids": sorted(baselined_target_ids or set()),
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def rail_alert_type(
    item: object,
    previous_signature: str | None,
    previous_quality: int | None,
) -> str:
    quality = rail_quality(item)
    if quality <= 0:
        return "예약대기"
    if previous_signature == UNAVAILABLE_SIGNATURE or (
        previous_signature is not None and previous_signature.startswith("waitlist|")
    ):
        return "예약 가능"
    if previous_signature is None:
        if previous_quality is not None and previous_quality > 0 and quality > previous_quality:
            return "좌석 품질 상승"
        return "예약 가능"
    if previous_quality is not None and quality > previous_quality:
        return "좌석 품질 상승"
    return "좌석 변동"


def collect_bus(config: dict) -> list[tuple[dict, list[BusCandidate]]]:
    scans: list[tuple[dict, list[BusCandidate]]] = []
    cache: dict[tuple[str, str, str, str, str], list[BusCandidate]] = {}
    reused = 0
    for target in config.get("bus_targets", []):
        query_key = target_query_key(target)
        if query_key in cache:
            items = cache[query_key]
            reused += 1
        else:
            try:
                items = search_user_bus_routes(
                    target["departure"], target["arrival"], target["date"],
                    target["start"], target["end"],
                    include_unavailable=True,
                )
            except Exception as exc:
                print(f"WARNING bus target={target.get('id', '?')}: {exc}")
                items = []
            cache[query_key] = items
        scans.append((target, items))
    if reused:
        print(f"BUS_QUERY_REUSE exact={reused}")
    return scans


def collect_rail(
    config: dict,
    *,
    debug: bool = False,
) -> tuple[list[object], set[str]]:
    targets = config.get("rail_targets", [])
    if not targets:
        return [], set()
    if not os.getenv("SEATWATCHER_KORAIL_MEMBER_NO") or not os.getenv("SEATWATCHER_KORAIL_PASSWORD"):
        print("KORAIL_SKIPPED credentials_missing")
        return [], set()

    unique_targets: list[dict] = []
    representative_by_query: dict[tuple[str, str, str, str, str], str] = {}
    aliases_by_representative: dict[str, list[str]] = {}
    for target in targets:
        target_id = str(target["id"])
        query_key = target_query_key(target)
        representative = representative_by_query.get(query_key)
        if representative is None:
            representative_by_query[query_key] = target_id
            aliases_by_representative[target_id] = [target_id]
            unique_targets.append(target)
        elif target_id not in aliases_by_representative[representative]:
            aliases_by_representative[representative].append(target_id)

    reused = len(targets) - len(unique_targets)
    if reused:
        print(f"KORAIL_QUERY_REUSE exact={reused}")

    try:
        from rail_provider import search_korail_targets

        representative_status: dict[str, bool] = {}
        rows = search_korail_targets(
            unique_targets,
            debug=debug,
            transfer_display_direct_threshold=int(
                config.get("transfer_display_direct_threshold", 3)
            ),
            target_status=representative_status,
        )
        expanded: list[object] = []
        for item in rows:
            aliases = aliases_by_representative.get(str(item.target_id), [str(item.target_id)])
            for target_id in aliases:
                expanded.append(
                    item if target_id == str(item.target_id) else replace(item, target_id=target_id)
                )

        successful_targets: set[str] = set()
        for representative, aliases in aliases_by_representative.items():
            if representative_status.get(representative, False):
                successful_targets.update(aliases)
        return expanded, successful_targets
    except Exception as exc:
        print(f"WARNING RAIL cycle: {exc}")
        return [], set()


def _is_auto_waitlist_candidate(item: object, target: dict, notify: bool) -> bool:
    return (
        notify
        and bool(target.get("auto_waitlist"))
        and getattr(item, "kind", "") == "DIRECT"
        and rail_quality(item) <= 0
        and "예약대기 가능" in str(getattr(item, "seat_text", "") or "")
    )


def _run_auto_waitlist_fast_path(
    rails: list[object],
    rail_target_by_id: dict[str, dict],
    notify: bool,
) -> tuple[dict[str, object | None], set[str]]:
    """Apply standby before unrelated bus/result processing can delay the mutation.

    Candidates are already sorted by departure time. For each target, safe
    preflight failures may fall through to the next train, but once a mutation
    was attempted or an existing/successful standby is confirmed, later
    candidates for that target are suppressed for this cycle.
    """
    outcomes: dict[str, object | None] = {}
    claimed_target_ids: set[str] = set()
    if not notify:
        return outcomes, claimed_target_ids

    for item in rails:
        target_id = str(getattr(item, "target_id", "") or "")
        if target_id in claimed_target_ids:
            continue
        target = rail_target_by_id.get(target_id, {})
        if not _is_auto_waitlist_candidate(item, target, notify):
            continue

        item_key = rail_key(item)
        started = time.perf_counter()
        outcome = attempt_auto_waitlist(item, target, notify_result=True)
        outcomes[item_key] = outcome
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        if outcome is None:
            print(
                f"AUTO_WAITLIST_FAST status=skipped target={target_id} "
                f"latency_ms={elapsed_ms:.0f}"
            )
            continue

        print(
            f"AUTO_WAITLIST_FAST status={outcome.status} "
            f"success={outcome.success} attempted={outcome.attempted} "
            f"train={outcome.train_no} dep={outcome.departure_time} "
            f"latency_ms={elapsed_ms:.0f}"
        )
        if (
            outcome.attempted
            or outcome.success
            or outcome.status in {"already_present", "success_recovered", "ambiguous_failure"}
        ):
            claimed_target_ids.add(target_id)

    return outcomes, claimed_target_ids


def run_once(config: dict, notify: bool, *, rail_debug: bool = False) -> None:
    cycle_started = time.perf_counter()
    if notify:
        retry_result = retry_pending_waitlist_notifications()
        if retry_result["attempted"]:
            print(
                "AUTO_WAITLIST PUSHOVER_RETRY "
                f"attempted={retry_result['attempted']} "
                f"sent={retry_result['sent']} failed={retry_result['failed']}"
            )
    state = load_json(
        STATE,
        {
            "notified_keys": [],
            "bus_availability_by_journey": {},
            "rail_availability_by_journey": {},
            "rail_quality_by_journey": {},
            "baselined_target_ids": [],
        },
    )
    notified = set(state.get("notified_keys", []))
    previous_bus_availability = {
        str(key): str(value)
        for key, value in state.get("bus_availability_by_journey", {}).items()
    }
    previous_rail_availability = {
        str(key): str(value)
        for key, value in state.get("rail_availability_by_journey", {}).items()
    }
    previous_rail_quality = {
        str(key): int(value)
        for key, value in state.get("rail_quality_by_journey", {}).items()
        if str(value).isdigit()
    }
    baselined_target_ids = {str(value) for value in state.get("baselined_target_ids", [])}
    baseline_target_ids = {
        str(target["id"])
        for target in config.get("rail_targets", [])
        if target.get("suppress_initial_alert")
    }
    unbaselined_target_ids = baseline_target_ids - baselined_target_ids
    current_bus_availability = dict(previous_bus_availability)
    current_rail_availability = dict(previous_rail_availability)
    current_rail_quality = dict(previous_rail_quality)
    alertable_now: set[str] = set()
    sent: set[str] = set()
    pending_events: list[AlertEvent] = []
    rail_target_by_id = {
        str(target["id"]): target
        for target in config.get("rail_targets", [])
    }

    # 버스와 철도는 서로 다른 서비스이므로 네트워크 대기를 겹쳐도
    # 동일 공급자에 대한 요청 빈도는 늘지 않는다.
    with ThreadPoolExecutor(max_workers=2) as executor:
        bus_future = executor.submit(collect_bus, config)
        rails, successful_rail_targets = collect_rail(config, debug=rail_debug)

        # Reservation standby is latency-sensitive. Execute it immediately
        # after the KORAIL result is available instead of waiting for unrelated
        # bus I/O, printing, state bookkeeping, or bundled notifications.
        auto_waitlist_outcomes, auto_waitlist_claimed_targets = (
            _run_auto_waitlist_fast_path(rails, rail_target_by_id, notify)
        )
        bus_scans = bus_future.result()

    buses: list[tuple[str, BusCandidate]] = []
    bus_total = len(bus_scans)
    for index, (target, items) in enumerate(bus_scans, 1):
        target_id = str(target["id"])
        date = str(target["date"])
        date_text = f"{date[4:6]}/{date[6:8]}"
        start_time = str(target["start"])
        end_time = str(target["end"])
        start_text = f"{start_time[:2]}:{start_time[2:4]}"
        end_text = f"{end_time[:2]}:{end_time[2:4]}"
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

            item_key = bus_key(target_id, item)
            current_signature = bus_availability_signature(item)
            previous_signature = previous_bus_availability.get(item_key)
            legacy_seen = previous_signature is None and item_key in notified

            if not item.bookable:
                current_bus_availability[item_key] = UNAVAILABLE_SIGNATURE
                continue

            buses.append((target_id, item))
            safe, reason = bus_last_mile(item)
            if not safe:
                current_bus_availability[item_key] = current_signature
                print(f"    ! 최종교통 제외: {reason}")
                continue

            alertable_now.add(item_key)
            alert_type: str | None = None
            if previous_signature is None:
                if not legacy_seen:
                    alert_type = "예약 가능"
            elif previous_signature == UNAVAILABLE_SIGNATURE:
                alert_type = "예약 가능"
            elif previous_signature != current_signature:
                alert_type = "좌석 변동"

            if alert_type is not None:
                marker = "NEW" if alert_type == "예약 가능" else "CHANGED"
                print(f"    + {marker} | {availability} | {reason}")
                pending_events.append(
                    build_bus_event(
                        key=item_key,
                        item=item,
                        alert_type=alert_type,
                        previous_signature=previous_signature,
                    )
                )
            current_bus_availability[item_key] = current_signature

        print(f"  -> 완료 | 배차 {len(items)}편 | 현재 예약 가능 {available_count}편")

    rail_direct = sum(1 for item in rails if item.kind == "DIRECT")
    rail_transfer = sum(1 for item in rails if item.kind == "TRANSFER")
    print(f"기차 이용 가능 후보 | 직통 {rail_direct} | 환승 {rail_transfer}")
    current_rail_keys: set[str] = set()
    for item in rails:
        item_key = rail_key(item)
        current_rail_keys.add(item_key)
        quality = rail_quality(item)
        current_signature = rail_availability_signature(item)
        previous_signature = previous_rail_availability.get(item_key)
        previous_quality = previous_rail_quality.get(item_key)
        legacy_seen = previous_signature is None and (
            item_key in notified or previous_quality is not None
        )
        is_new = previous_signature is None and not legacy_seen
        reopened = previous_signature == UNAVAILABLE_SIGNATURE
        signature_changed = (
            previous_signature is not None
            and previous_signature != current_signature
        )
        legacy_improved = (
            previous_signature is None
            and previous_quality is not None
            and previous_quality > 0
            and quality > previous_quality
        )
        currently_bookable = quality > 0
        if currently_bookable:
            should_alert = is_new or reopened or signature_changed or legacy_improved
        else:
            # 예약대기는 별도 상태다. 신규/매진 후 예약대기 진입만 알리고,
            # 실제 좌석에서 예약대기로 악화된 사실은 알리지 않는다.
            should_alert = is_new or reopened

        is_initial_baseline = (
            item.target_id in unbaselined_target_ids
            and previous_signature is None
            and not legacy_seen
        )
        if is_initial_baseline:
            should_alert = False

        alert_type = rail_alert_type(item, previous_signature, previous_quality)
        safe, reason = rail_last_mile(item)
        if not safe:
            marker = "BLOCKED_LASTMILE"
        elif is_initial_baseline:
            marker = "BASELINE"
        elif should_alert:
            marker = alert_type.replace(" ", "_")
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
            current_rail_availability[item_key] = current_signature
            current_rail_quality[item_key] = quality
            continue

        target = rail_target_by_id.get(str(item.target_id), {})
        auto_waitlist_candidate = _is_auto_waitlist_candidate(item, target, notify)
        if auto_waitlist_candidate:
            # The fast path above normally handled this before bus/result work.
            # Fallback here only if a future caller bypasses that path.
            if (
                item_key not in auto_waitlist_outcomes
                and str(item.target_id) not in auto_waitlist_claimed_targets
            ):
                outcome = attempt_auto_waitlist(
                    item,
                    target,
                    notify_result=True,
                )
                auto_waitlist_outcomes[item_key] = outcome
                if outcome is not None:
                    print(
                        f"AUTO_WAITLIST_FALLBACK status={outcome.status} "
                        f"success={outcome.success} attempted={outcome.attempted} "
                        f"train={outcome.train_no} dep={outcome.departure_time}"
                    )
            # 예약대기 가능 상태는 일반 좌석 알림 대신 자동 신청 결과만 알린다.
            current_rail_availability[item_key] = current_signature
            current_rail_quality[item_key] = quality
            alertable_now.add(item_key)
            continue

        alertable_now.add(item_key)
        if should_alert:
            print(f"    + {alert_type} | {item.seat_text}")
            pending_events.append(
                build_rail_event(
                    key=item_key,
                    item=item,
                    alert_type=alert_type,
                    previous_signature=previous_signature,
                )
            )
        current_rail_availability[item_key] = current_signature
        current_rail_quality[item_key] = quality

    known_rail_keys = (
        set(previous_rail_availability)
        | set(previous_rail_quality)
        | {key for key in notified if is_rail_state_key(key)}
    )
    for item_key in known_rail_keys - current_rail_keys:
        if state_target_id(item_key) not in successful_rail_targets:
            continue
        current_rail_availability[item_key] = UNAVAILABLE_SIGNATURE
        current_rail_quality.pop(item_key, None)

    if notify and pending_events:
        try:
            send_alert_batch(
                pending_events,
                notification_config=config.get("notification", {}),
            )
        except Exception as exc:
            print(f"WARNING pushover_bundle count={len(pending_events)}: {exc}")
            for event in pending_events:
                if event.transport == "버스":
                    if event.key in previous_bus_availability:
                        current_bus_availability[event.key] = previous_bus_availability[event.key]
                    else:
                        current_bus_availability.pop(event.key, None)
                else:
                    if event.key in previous_rail_availability:
                        current_rail_availability[event.key] = previous_rail_availability[event.key]
                    else:
                        current_rail_availability.pop(event.key, None)
                    if event.key in previous_rail_quality:
                        current_rail_quality[event.key] = previous_rail_quality[event.key]
                    else:
                        current_rail_quality.pop(event.key, None)
        else:
            sent.update(event.key for event in pending_events)
            print(f"PUSHOVER_SENT_BUNDLE count={len(pending_events)}")

    if notify:
        active_bus_ids = {str(target["id"]) for target in config.get("bus_targets", [])}
        active_rail_ids = {str(target["id"]) for target in config.get("rail_targets", [])}
        current_bus_availability = {
            key: value
            for key, value in current_bus_availability.items()
            if state_target_id(key) in active_bus_ids
        }
        current_rail_availability = {
            key: value
            for key, value in current_rail_availability.items()
            if state_target_id(key) in active_rail_ids
        }
        current_rail_quality = {
            key: value
            for key, value in current_rail_quality.items()
            if state_target_id(key) in active_rail_ids
        }
        completed_baselines = baselined_target_ids | (
            unbaselined_target_ids & successful_rail_targets
        )
        save_state(
            (notified & alertable_now) | sent,
            current_bus_availability,
            current_rail_availability,
            current_rail_quality,
            completed_baselines,
        )
    elapsed = time.perf_counter() - cycle_started
    print(
        f"SUMMARY bus={len(buses)} rail={len(rails)} alertable={len(alertable_now)} "
        f"notify={notify} cycle_seconds={elapsed:.1f}"
    )

def send_pushover_test_alert() -> str:
    from pushover_notify import is_configured, send_message

    if not is_configured():
        raise RuntimeError(
            "Pushover 인증값이 없습니다. .env.local의 "
            "SEATWATCHER_PUSHOVER_APP_TOKEN/SEATWATCHER_PUSHOVER_USER_KEY를 확인하세요."
        )

    page_url = build_pushover_alert_page_url(demo_alert_events())
    send_message(
        "SeatWatcher Pushover 진동 테스트입니다. 실제 좌석 변동 알림이 아닙니다.",
        link_url=page_url,
        sound="vibrate",
        title="SeatWatcher 진동 테스트",
    )
    print("PUSHOVER_TEST_SENT sound=vibrate priority=0")
    print(f"DETAIL_PAGE {page_url}")
    return page_url


def main() -> int:
    load_project_env()
    parser = argparse.ArgumentParser(description="SeatWatcher recurring watcher")
    parser.add_argument("--watch", action="store_true", help="설정된 주기로 계속 감시")
    parser.add_argument("--notify", action="store_true", help="새 후보를 Pushover로 알림")
    parser.add_argument("--rail-debug", action="store_true", help="KORAIL 상세 진단 로그 표시")
    parser.add_argument("--test-alert", action="store_true", help="실제 조회 없이 Pushover 묶음 알림/Pages 링크 테스트")
    parser.add_argument("--test-pushover", action="store_true", help="Pushover 진동 테스트 1회 전송")
    parser.add_argument(
        "--wide-rail-test",
        action="store_true",
        help="저장 설정은 건드리지 않고 KORAIL 테스트 날짜/시간 범위를 임시 확대",
    )
    args = parser.parse_args()
    config = load_config()
    if args.test_pushover:
        send_pushover_test_alert()
        return 0
    if args.test_alert:
        page_url = send_alert_batch(
            demo_alert_events(),
            notification_config=config.get("notification", {}),
        )
        print("PUSHOVER_TEST_ALERT_SENT")
        print(f"DETAIL_PAGE {page_url}")
        return 0
    if args.wide_rail_test:
        config = expand_rail_targets_for_wide_test(config)

    base_interval = max(60, int(config.get("poll_interval_seconds", 120)))
    auto_waitlist_enabled = any(
        bool(target.get("auto_waitlist"))
        for target in config.get("rail_targets", [])
    )
    if auto_waitlist_enabled:
        try:
            requested_fast_interval = int(
                config.get("auto_waitlist_poll_interval_seconds", 30)
            )
        except (TypeError, ValueError):
            requested_fast_interval = 30
        fast_interval = min(60, max(20, requested_fast_interval))
        interval = min(base_interval, fast_interval)
        print(
            f"AUTO_WAITLIST_FAST_POLL interval={interval}s "
            f"base_interval={base_interval}s"
        )
    else:
        interval = base_interval

    while True:
        cycle_started = time.perf_counter()
        try:
            run_once(config, args.notify, rail_debug=args.rail_debug)
        except Exception as exc:
            print(f"ERROR cycle: {exc}")
        if not args.watch:
            return 0
        elapsed = time.perf_counter() - cycle_started
        sleep_seconds = max(0.0, interval - elapsed)
        print(
            f"NEXT_POLL target_interval={interval}s "
            f"cycle_seconds={elapsed:.1f} sleep_seconds={sleep_seconds:.1f}"
        )
        try:
            time.sleep(sleep_seconds)
        except KeyboardInterrupt:
            print("SeatWatcher stopped")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
