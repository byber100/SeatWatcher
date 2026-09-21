from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from alert_bundle import DEFAULT_ALERT_PAGE_URL
from env_loader import load_project_env

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / ".runtime" / "standby_reservation_state.json"
WAITLIST_FLAG = " 9"
DEFAULT_RETRY_SECONDS = 120
DEFAULT_MAX_SAFE_RETRIES = 3


def _patch_train_class_code_validation() -> None:
    """Backport upstream KTX alphanumeric train-class validation for reservations.

    korail-mobile-api 1.1.1 rejects live KTX class codes such as ``0A`` because
    its private reservation helper treats every class code as decimal digits.
    Upstream later corrected only this field to opaque text. Keep every other
    numeric validation unchanged and permit a conservative ASCII alphanumeric
    class code here.
    """
    import re
    import korail_mobile_api.mutation_payloads as payloads

    current = payloads._required_digits
    if getattr(current, "_seatwatcher_train_class_patch", False):
        return

    def patched(value: str | None, *, field: str) -> str:
        if field == "train_class_code":
            if not isinstance(value, str) or re.fullmatch(r"[0-9A-Za-z]{1,8}", value) is None:
                from korail_mobile_api import KorailProtocolError

                raise KorailProtocolError(
                    "KORAIL reservation train field train_class_code must be "
                    "a short ASCII alphanumeric code"
                )
            return value
        return current(value, field=field)

    patched._seatwatcher_train_class_patch = True  # type: ignore[attr-defined]
    payloads._required_digits = patched

    import korail_mobile_api.client as client_module

    history_parser = client_module.parse_reservation_history_response
    if not getattr(history_parser, "_seatwatcher_history_scalar_patch", False):
        def patched_history_parser(raw: dict[str, Any]):
            if isinstance(raw, dict) and type(raw.get("h_jrny_cnt")) is int:
                raw = dict(raw)
                raw["h_jrny_cnt"] = str(raw["h_jrny_cnt"])
            return history_parser(raw)

        patched_history_parser._seatwatcher_history_scalar_patch = True  # type: ignore[attr-defined]
        client_module.parse_reservation_history_response = patched_history_parser


@dataclass(frozen=True)
class StandbyOutcome:
    key: str
    status: str
    attempted: bool
    success: bool
    message: str
    train_no: str
    departure_time: str
    verified_in_history: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _utc_now()).isoformat(timespec="seconds")


def _read_state() -> dict[str, Any]:
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        payload = {}
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, dict):
        records = {}
    return {"records": records}


def _write_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(STATE_PATH)


def _norm_train_no(value: object) -> str:
    text = str(value or "").strip()
    return text.lstrip("0") or text


def _norm_time(value: object) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isdigit())
    return text.zfill(6)[-6:] if text else ""


def standby_key(candidate: object) -> str:
    return "|".join(
        (
            str(getattr(candidate, "target_id", "")),
            str(getattr(candidate, "date", "")),
            _norm_train_no(getattr(candidate, "train_no", "")),
            _norm_time(getattr(candidate, "departure_time", "")),
            str(getattr(candidate, "departure_station", "")),
            str(getattr(candidate, "arrival_station", "")),
        )
    )


def _history_matches(candidate: object, item: object) -> bool:
    candidate_train_no = _norm_train_no(getattr(candidate, "train_no", ""))
    history_train_no = _norm_train_no(getattr(item, "train_no", ""))
    if candidate_train_no and history_train_no != candidate_train_no:
        return False
    if str(getattr(item, "run_date", "") or "") != str(getattr(candidate, "date", "")):
        return False
    if _norm_time(getattr(item, "departure_time", "")) != _norm_time(
        getattr(candidate, "departure_time", "")
    ):
        return False
    departure = str(getattr(item, "departure_station", "") or "").strip()
    arrival = str(getattr(item, "arrival_station", "") or "").strip()
    if departure and departure != str(getattr(candidate, "departure_station", "")).strip():
        return False
    if arrival and arrival != str(getattr(candidate, "arrival_station", "")).strip():
        return False
    return True


def _find_history_match(client: object, candidate: object) -> object | None:
    history = client.get_reservation_history()
    for item in getattr(history, "trains", ()) or ():
        if _history_matches(candidate, item):
            return item
    return None


def _find_fresh_train(client: object, candidate: object) -> object | None:
    import korail_mobile_api as api

    query = api.TrainSearchQuery(
        str(getattr(candidate, "departure_station_code", "") or getattr(candidate, "departure_station", "")),
        str(getattr(candidate, "arrival_station_code", "") or getattr(candidate, "arrival_station", "")),
        str(getattr(candidate, "date", "")),
        departure_time=_norm_time(getattr(candidate, "departure_time", "")),
        passengers=1,
        train_group_code="109",
        include_srt=True,
    )
    continuation = None
    for _ in range(4):
        result = client.search_trains(query, continuation=continuation)
        for train in result.trains:
            if (
                _norm_train_no(getattr(train, "train_no", ""))
                == _norm_train_no(getattr(candidate, "train_no", ""))
                and _norm_time(getattr(train, "departure_time", ""))
                == _norm_time(getattr(candidate, "departure_time", ""))
            ):
                return train
        continuation = result.next_page()
        if continuation is None:
            break
    return None


def _notify(candidate: object, outcome: StandbyOutcome) -> None:
    from pushover_notify import is_configured, send_message

    if not is_configured():
        raise RuntimeError("Pushover is not configured")
    date = str(getattr(candidate, "date", ""))
    date_text = f"{date[4:6]}/{date[6:8]}" if len(date) == 8 else date
    dep = _norm_time(getattr(candidate, "departure_time", ""))
    dep_text = f"{dep[:2]}:{dep[2:4]}" if len(dep) >= 4 else dep
    route = (
        f"{getattr(candidate, 'departure_station', '')}→"
        f"{getattr(candidate, 'arrival_station', '')}"
    )
    prefix = "성공" if outcome.success else "실패"
    message = (
        f"예약대기 자동신청 {prefix}\n"
        f"{date_text} {dep_text} {route}\n"
        f"{getattr(candidate, 'train_text', '')}\n"
        f"{outcome.message}"
    )
    link_url = os.getenv("SEATWATCHER_ALERT_PAGE_URL", "").strip() or DEFAULT_ALERT_PAGE_URL
    send_message(
        message,
        link_url=link_url,
        sound="vibrate",
        title=f"SeatWatcher 예약대기 {prefix}",
    )


def _notification_candidate_snapshot(candidate: object) -> dict[str, str]:
    """Keep only non-sensitive fields needed to rebuild a failed Pushover result."""
    return {
        "date": str(getattr(candidate, "date", "") or ""),
        "departure_station": str(getattr(candidate, "departure_station", "") or ""),
        "arrival_station": str(getattr(candidate, "arrival_station", "") or ""),
        "departure_time": _norm_time(getattr(candidate, "departure_time", "")),
        "train_text": str(getattr(candidate, "train_text", "") or ""),
    }


def _outcome_from_record(key: str, record: dict[str, Any]) -> StandbyOutcome:
    return StandbyOutcome(
        key=key,
        status=str(record.get("status") or "unknown"),
        attempted=bool(record.get("attempted", False)),
        success=bool(record.get("success")),
        message=str(record.get("message") or "예약대기 처리 결과입니다."),
        train_no=str(record.get("train_no") or ""),
        departure_time=str(record.get("departure_time") or ""),
        verified_in_history=bool(record.get("verified_in_history")),
    )


def _notification_retry_due(record: dict[str, Any], now: datetime) -> bool:
    last_attempt = str(record.get("last_notification_attempt_at_utc") or "")
    if not last_attempt:
        return True
    try:
        attempted_at = datetime.fromisoformat(last_attempt.replace("Z", "+00:00"))
    except ValueError:
        return True
    return now >= attempted_at + timedelta(seconds=DEFAULT_RETRY_SECONDS)


def _record_outcome(
    state: dict[str, Any],
    outcome: StandbyOutcome,
    *,
    notify_candidate: object | None = None,
    terminal: bool,
    next_retry_seconds: int | None = None,
) -> StandbyOutcome:
    """Persist the reservation result separately from notification delivery.

    The reservation result may be terminal while Pushover is still pending.
    Persist pending state before the external notification call so a process
    crash cannot silently lose the result alert.
    """
    now = _utc_now()
    records = state.setdefault("records", {})
    previous = records.get(outcome.key)
    attempts = int(previous.get("attempts", 0)) if isinstance(previous, dict) else 0
    if outcome.attempted:
        attempts += 1

    record = {
        "status": outcome.status,
        "success": outcome.success,
        "terminal": terminal,
        "attempted": outcome.attempted,
        "attempts": attempts,
        "last_attempt_at_utc": _iso(now),
        "message": outcome.message,
        "train_no": outcome.train_no,
        "departure_time": outcome.departure_time,
        "verified_in_history": outcome.verified_in_history,
    }
    if next_retry_seconds is not None:
        record["next_retry_at_utc"] = _iso(now + timedelta(seconds=next_retry_seconds))

    notified_status = previous.get("notified_status") if isinstance(previous, dict) else None
    if notified_status:
        record["notified_status"] = notified_status

    if notify_candidate is not None and notified_status != outcome.status:
        record["notification_pending"] = True
        record["notification_candidate"] = _notification_candidate_snapshot(notify_candidate)
        record["last_notification_attempt_at_utc"] = _iso(now)

        # Write before calling Pushover. If the process dies mid-request, the
        # next watcher cycle retries only the alert, never the reservation.
        records[outcome.key] = record
        _write_state(state)

        try:
            _notify(notify_candidate, outcome)
        except Exception as exc:
            record["last_notification_error_type"] = type(exc).__name__
            print(
                f"WARNING AUTO_WAITLIST pushover_pending "
                f"status={outcome.status} error={type(exc).__name__}",
                flush=True,
            )
        else:
            record["notified_status"] = outcome.status
            record["notification_pending"] = False
            record.pop("last_notification_error_type", None)

        records[outcome.key] = record
        _write_state(state)
        return outcome

    if isinstance(previous, dict) and previous.get("notification_pending"):
        # A caller that intentionally suppresses notification must not erase a
        # previously queued delivery attempt.
        record["notification_pending"] = True
        if isinstance(previous.get("notification_candidate"), dict):
            record["notification_candidate"] = previous["notification_candidate"]
        if previous.get("last_notification_attempt_at_utc"):
            record["last_notification_attempt_at_utc"] = previous[
                "last_notification_attempt_at_utc"
            ]
        if previous.get("last_notification_error_type"):
            record["last_notification_error_type"] = previous[
                "last_notification_error_type"
            ]

    records[outcome.key] = record
    _write_state(state)
    return outcome


def retry_pending_waitlist_notifications() -> dict[str, int]:
    """Retry failed terminal-result alerts without touching KORAIL mutations."""
    state = _read_state()
    records = state.get("records", {})
    now = _utc_now()
    attempted = 0
    sent = 0
    failed = 0

    for key, record in records.items():
        if not isinstance(record, dict) or not bool(record.get("notification_pending")):
            continue

        status = str(record.get("status") or "")
        if record.get("notified_status") == status:
            record["notification_pending"] = False
            _write_state(state)
            continue
        if not _notification_retry_due(record, now):
            continue

        snapshot = record.get("notification_candidate")
        if not isinstance(snapshot, dict):
            failed += 1
            record["last_notification_error_type"] = "MissingNotificationSnapshot"
            _write_state(state)
            continue

        candidate = SimpleNamespace(**snapshot)
        outcome = _outcome_from_record(str(key), record)
        attempted += 1
        record["last_notification_attempt_at_utc"] = _iso(now)
        _write_state(state)

        try:
            _notify(candidate, outcome)
        except Exception as exc:
            failed += 1
            record["last_notification_error_type"] = type(exc).__name__
            print(
                f"WARNING AUTO_WAITLIST pushover_retry_pending "
                f"status={status} error={type(exc).__name__}",
                flush=True,
            )
        else:
            sent += 1
            record["notified_status"] = status
            record["notification_pending"] = False
            record.pop("last_notification_error_type", None)
            print(
                f"AUTO_WAITLIST PUSHOVER_RETRY_SENT status={status}",
                flush=True,
            )
        _write_state(state)

    return {"attempted": attempted, "sent": sent, "failed": failed}


def _retry_allowed(record: dict[str, Any], max_retries: int) -> bool:
    if bool(record.get("terminal")):
        return False
    if int(record.get("attempts", 0) or 0) >= max_retries:
        return False
    retry_at = str(record.get("next_retry_at_utc") or "")
    if retry_at:
        try:
            when = datetime.fromisoformat(retry_at.replace("Z", "+00:00"))
        except ValueError:
            when = None
        if when is not None and _utc_now() < when:
            return False
    return True


def attempt_auto_waitlist(
    candidate: object,
    target: dict[str, Any],
    *,
    notify_result: bool = True,
) -> StandbyOutcome | None:
    """Reserve KORAIL standby first, then report success/failure.

    Only direct candidates whose read result is exactly waitlist-only are accepted.
    Payment is never attempted here.
    """
    if not bool(target.get("auto_waitlist")):
        return None
    if str(getattr(candidate, "kind", "")) != "DIRECT":
        return None
    if int(getattr(candidate, "first_availability_rank", 0) or 0) > 0:
        return None
    if "예약대기 가능" not in str(getattr(candidate, "seat_text", "")):
        return None

    key = standby_key(candidate)
    train_no = _norm_train_no(getattr(candidate, "train_no", ""))
    departure_time = _norm_time(getattr(candidate, "departure_time", ""))
    if not train_no:
        outcome = StandbyOutcome(
            key, "invalid_candidate", False, False,
            "열차번호가 없어 자동신청을 중단했습니다.", train_no, departure_time,
        )
        state = _read_state()
        return _record_outcome(
            state, outcome,
            notify_candidate=candidate if notify_result else None,
            terminal=True,
        )

    state = _read_state()
    record = state["records"].get(key)
    max_retries = max(1, int(target.get("auto_waitlist_max_safe_retries", DEFAULT_MAX_SAFE_RETRIES)))
    if isinstance(record, dict) and not _retry_allowed(record, max_retries):
        return StandbyOutcome(
            key,
            str(record.get("status") or "suppressed"),
            False,
            bool(record.get("success")),
            str(record.get("message") or "이미 처리된 예약대기 후보입니다."),
            train_no,
            departure_time,
            bool(record.get("verified_in_history")),
        )

    load_project_env()
    member_no = os.getenv("SEATWATCHER_KORAIL_MEMBER_NO", "").strip()
    password = os.getenv("SEATWATCHER_KORAIL_PASSWORD", "")
    if not member_no or not password:
        outcome = StandbyOutcome(
            key, "credentials_missing", False, False,
            "KORAIL 회원번호/비밀번호가 없어 자동신청할 수 없습니다.",
            train_no, departure_time,
        )
        return _record_outcome(
            state, outcome,
            notify_candidate=candidate if notify_result else None,
            terminal=False,
            next_retry_seconds=DEFAULT_RETRY_SECONDS,
        )

    import korail_mobile_api as api

    _patch_train_class_code_validation()
    client = api.KorailClient(api.KorailConfig(enable_dynapath=True))
    stage = "login"
    mutation_started = False
    try:
        client.login(member_no, password)

        stage = "history_precheck"
        existing = _find_history_match(client, candidate)
        if existing is not None:
            outcome = StandbyOutcome(
                key, "already_present", False, True,
                "같은 열차가 이미 예약내역에 있어 중복 신청하지 않았습니다.",
                train_no, departure_time, True,
            )
            return _record_outcome(
                state, outcome,
                notify_candidate=candidate if notify_result else None,
                terminal=True,
            )

        stage = "fresh_search"
        train = _find_fresh_train(client, candidate)
        if train is None:
            outcome = StandbyOutcome(
                key, "fresh_train_missing", False, False,
                "신청 직전 재조회에서 같은 열차를 찾지 못했습니다. 다음 감시에서 다시 확인합니다.",
                train_no, departure_time,
            )
            return _record_outcome(
                state, outcome,
                notify_candidate=candidate if notify_result else None,
                terminal=False,
                next_retry_seconds=DEFAULT_RETRY_SECONDS,
            )
        if str(getattr(train, "wait_reservation_flag", "") or "") != WAITLIST_FLAG:
            outcome = StandbyOutcome(
                key, "waitlist_closed", False, False,
                "신청 직전 재조회에서 예약대기 가능 상태가 사라졌습니다. 다시 열리면 재시도합니다.",
                train_no, departure_time,
            )
            return _record_outcome(
                state, outcome,
                notify_candidate=candidate if notify_result else None,
                terminal=False,
                next_retry_seconds=DEFAULT_RETRY_SECONDS,
            )

        consent = api.MutationConsent(allow_reserve=True, dry_run=False)

        stage = "reserve_standby"
        mutation_started = True
        hold = client.reserve(
            train,
            consent=consent,
            passengers=api.KorailPassengerCounts(adult=1),
            seat_class=api.KorailSeatClass.GENERAL,
            job_type=api.KorailReservationJobType.STANDBY,
        )
        if str(getattr(hold, "str_result", "") or "") != "SUCC":
            raise RuntimeError(
                f"예약대기 홀드 실패: {getattr(hold, 'h_msg_cd', '')} "
                f"{getattr(hold, 'h_msg_txt', '')}"
            )

        stage = "confirm_standby"
        confirmed = client.confirm_standby_hold(
            hold,
            consent=consent,
            allow_seat_class_change=bool(target.get("waitlist_allow_seat_class_change", False)),
            sms_notify=False,
        )
        if str(getattr(confirmed, "str_result", "") or "") != "SUCC":
            raise RuntimeError(
                f"예약대기 확정 실패: {getattr(confirmed, 'h_msg_cd', '')} "
                f"{getattr(confirmed, 'h_msg_txt', '')}"
            )

        stage = "history_postcheck"
        verified = _find_history_match(client, candidate) is not None
        detail = "예약대기 신청이 완료되었습니다."
        if verified:
            detail += " 예약내역에서도 확인했습니다."
        else:
            detail += " 서버 성공 응답을 받았지만 예약내역 즉시 재조회에는 아직 나타나지 않았습니다."
        outcome = StandbyOutcome(
            key, "success", True, True, detail, train_no, departure_time, verified,
        )
        return _record_outcome(
            state, outcome,
            notify_candidate=candidate if notify_result else None,
            terminal=True,
        )
    except Exception as exc:
        verified = False
        if mutation_started:
            try:
                verified = _find_history_match(client, candidate) is not None
            except Exception:
                verified = False
        if verified:
            outcome = StandbyOutcome(
                key, "success_recovered", True, True,
                f"{stage} 단계에서 예외가 있었지만 예약내역에서 같은 열차를 확인했습니다.",
                train_no, departure_time, True,
            )
            return _record_outcome(
                state, outcome,
                notify_candidate=candidate if notify_result else None,
                terminal=True,
            )

        if mutation_started:
            outcome = StandbyOutcome(
                key, "ambiguous_failure", True, False,
                f"{stage} 단계 실패 후 예약내역에서도 확인되지 않았습니다: "
                f"{type(exc).__name__}: {exc}. 중복 방지를 위해 자동 재신청은 중단합니다.",
                train_no, departure_time, False,
            )
            return _record_outcome(
                state, outcome,
                notify_candidate=candidate if notify_result else None,
                terminal=True,
            )

        outcome = StandbyOutcome(
            key, "preflight_failure", False, False,
            f"{stage} 단계에서 신청 전 실패했습니다: {type(exc).__name__}: {exc}",
            train_no, departure_time, False,
        )
        return _record_outcome(
            state, outcome,
            notify_candidate=candidate if notify_result else None,
            terminal=False,
            next_retry_seconds=DEFAULT_RETRY_SECONDS,
        )
    finally:
        try:
            client.logout()
        except Exception:
            pass
        client.close()


def dry_run_fixture(target: dict[str, Any]) -> dict[str, Any]:
    """Live login/read + synthetic waitlist flag; mutation requests are never sent."""
    load_project_env()
    member_no = os.getenv("SEATWATCHER_KORAIL_MEMBER_NO", "").strip()
    password = os.getenv("SEATWATCHER_KORAIL_PASSWORD", "")
    if not member_no or not password:
        raise RuntimeError("KORAIL credentials missing")

    import korail_mobile_api as api

    _patch_train_class_code_validation()
    client = api.KorailClient(api.KorailConfig(enable_dynapath=True))
    try:
        client.login(member_no, password)
        history = client.get_reservation_history()
        query = api.TrainSearchQuery(
            str(target["departure"]),
            str(target["arrival"]),
            str(target["date"]),
            departure_time=str(target["start"]),
            passengers=1,
            train_group_code="109",
            include_srt=True,
        )
        result = client.search_trains(query)
        trains = [
            train for train in result.trains
            if _norm_time(getattr(train, "departure_time", "")) <= str(target["end"])
        ]
        prefix = str(target.get("train_type_prefix", "") or "").upper()
        if prefix:
            trains = [
                train for train in trains
                if str(getattr(train, "train_class_name", "") or "").upper().startswith(prefix)
            ]
        if not trains:
            raise RuntimeError("dry-run target train not found")

        source = trains[0]
        synthetic = replace(source, wait_reservation_flag=WAITLIST_FLAG)
        consent = api.MutationConsent(allow_reserve=True, dry_run=True)
        reserve_preview = client.reserve(
            synthetic,
            consent=consent,
            passengers=api.KorailPassengerCounts(adult=1),
            seat_class=api.KorailSeatClass.GENERAL,
            job_type=api.KorailReservationJobType.STANDBY,
        )
        fake_hold = api.ReservationHoldResponse(
            str_result="SUCC",
            pnr_no="DRYRUN-PNR",
            journey_count="1",
        )
        confirm_preview = client.confirm_standby_hold(
            fake_hold,
            consent=consent,
            allow_seat_class_change=False,
            sms_notify=False,
        )
        return {
            "ok": True,
            "login": True,
            "history_count": len(getattr(history, "trains", ()) or ()),
            "train_no": _norm_train_no(getattr(source, "train_no", "")),
            "departure_time": _norm_time(getattr(source, "departure_time", "")),
            "reserve_route": getattr(reserve_preview, "route", ""),
            "confirm_route": getattr(confirm_preview, "route", ""),
            "reserve_payload_fields": len(getattr(reserve_preview, "payload", {}) or {}),
            "confirm_payload_fields": len(getattr(confirm_preview, "payload", {}) or {}),
            "mutations_sent": False,
        }
    finally:
        try:
            client.logout()
        except Exception:
            pass
        client.close()
