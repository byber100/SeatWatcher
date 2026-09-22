from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from alert_bundle import DEFAULT_ALERT_PAGE_URL
from env_loader import load_project_env
from standby_reservation import (
    _find_fresh_train,
    _find_history_match,
    _norm_time,
    _patch_train_class_code_validation,
)

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / ".runtime" / "auto_cart_reservation_state.json"
DEFAULT_RETRY_SECONDS = 30
TERMINAL_STATUSES = {
    "cart_confirmed",
    "already_reserved",
    "reservation_ambiguous",
    "cart_ambiguous",
}


@dataclass(frozen=True)
class CartReservationOutcome:
    key: str
    status: str
    attempted_reservation: bool
    attempted_cart: bool
    success: bool
    message: str
    train_no: str
    departure_time: str
    verified_in_history: bool = False
    verified_in_cart: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _norm(value: object) -> str:
    return "".join(ch for ch in str(value or "").strip() if ch.isalnum())


def reservation_key(candidate: object) -> str:
    raw = "|".join(
        (
            str(getattr(candidate, "date", "") or ""),
            str(getattr(candidate, "departure_station", "") or ""),
            str(getattr(candidate, "arrival_station", "") or ""),
            _norm(getattr(candidate, "train_no", "")).lstrip("0"),
            _norm_time(getattr(candidate, "departure_time", "")),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def is_designated_seat_candidate(candidate: object) -> bool:
    text = str(getattr(candidate, "seat_text", "") or "")
    return "일반실 좌석 가능" in text or "특실 좌석 가능" in text


def _read_state() -> dict[str, Any]:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"records": {}}
    if not isinstance(state, dict):
        return {"records": {}}
    if not isinstance(state.get("records"), dict):
        state["records"] = {}
    return state


def _write_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(STATE_FILE)


def _snapshot(candidate: object) -> dict[str, str]:
    return {
        "date": str(getattr(candidate, "date", "") or ""),
        "departure_station": str(getattr(candidate, "departure_station", "") or ""),
        "arrival_station": str(getattr(candidate, "arrival_station", "") or ""),
        "departure_time": _norm_time(getattr(candidate, "departure_time", "")),
        "train_text": str(getattr(candidate, "train_text", "") or ""),
    }


def _notify(candidate: object, outcome: CartReservationOutcome) -> None:
    from pushover_notify import is_configured, send_message

    load_project_env()
    if not is_configured():
        raise RuntimeError("Pushover is not configured")

    date = str(getattr(candidate, "date", "") or "")
    date_text = f"{date[4:6]}/{date[6:8]}" if len(date) == 8 else date
    dep = _norm_time(getattr(candidate, "departure_time", ""))
    dep_text = f"{dep[:2]}:{dep[2:4]}" if len(dep) >= 4 else dep
    route = f"{getattr(candidate, 'departure_station', '')}→{getattr(candidate, 'arrival_station', '')}"
    prefix = "성공" if outcome.success else "실패"
    body = (
        f"자동예약+장바구니 {prefix}\n"
        f"{date_text} {dep_text} {route}\n"
        f"{getattr(candidate, 'train_text', '')}\n"
        f"{outcome.message}"
    )
    link_url = os.getenv("SEATWATCHER_ALERT_PAGE_URL", "").strip() or DEFAULT_ALERT_PAGE_URL
    send_message(
        body,
        link_url=link_url,
        sound="vibrate",
        title=f"SeatWatcher 자동예약 {prefix}",
    )


def _outcome_from_record(key: str, record: dict[str, Any]) -> CartReservationOutcome:
    return CartReservationOutcome(
        key=key,
        status=str(record.get("status") or "unknown"),
        attempted_reservation=bool(record.get("attempted_reservation")),
        attempted_cart=bool(record.get("attempted_cart")),
        success=bool(record.get("success")),
        message=str(record.get("message") or "자동예약 처리 결과입니다."),
        train_no=str(record.get("train_no") or ""),
        departure_time=str(record.get("departure_time") or ""),
        verified_in_history=bool(record.get("verified_in_history")),
        verified_in_cart=bool(record.get("verified_in_cart")),
    )


def _record_outcome(
    candidate: object,
    outcome: CartReservationOutcome,
    *,
    terminal: bool,
    notify_result: bool,
    next_retry_seconds: int | None = None,
) -> CartReservationOutcome:
    state = _read_state()
    records = state["records"]
    previous = records.get(outcome.key)
    notified_status = previous.get("notified_status") if isinstance(previous, dict) else None
    now = _utc_now()
    record = {
        "status": outcome.status,
        "terminal": terminal,
        "attempted_reservation": outcome.attempted_reservation,
        "attempted_cart": outcome.attempted_cart,
        "success": outcome.success,
        "message": outcome.message,
        "train_no": outcome.train_no,
        "departure_time": outcome.departure_time,
        "verified_in_history": outcome.verified_in_history,
        "verified_in_cart": outcome.verified_in_cart,
        "updated_at_utc": _iso(now),
    }
    if next_retry_seconds is not None:
        record["next_retry_at_utc"] = _iso(now + timedelta(seconds=next_retry_seconds))
    if notified_status:
        record["notified_status"] = notified_status

    if notify_result and notified_status != outcome.status:
        record["notification_pending"] = True
        record["notification_candidate"] = _snapshot(candidate)
        record["last_notification_attempt_at_utc"] = _iso(now)
        records[outcome.key] = record
        _write_state(state)
        try:
            _notify(candidate, outcome)
        except Exception as exc:
            record["last_notification_error_type"] = type(exc).__name__
            print(
                f"WARNING AUTO_CART pushover_pending status={outcome.status} "
                f"error={type(exc).__name__}",
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
        record["notification_pending"] = True
        if isinstance(previous.get("notification_candidate"), dict):
            record["notification_candidate"] = previous["notification_candidate"]
        if previous.get("last_notification_attempt_at_utc"):
            record["last_notification_attempt_at_utc"] = previous["last_notification_attempt_at_utc"]
        if previous.get("last_notification_error_type"):
            record["last_notification_error_type"] = previous["last_notification_error_type"]

    records[outcome.key] = record
    _write_state(state)
    return outcome


def retry_pending_cart_notifications() -> dict[str, int]:
    state = _read_state()
    now = _utc_now()
    attempted = sent = failed = 0
    for key, record in state["records"].items():
        if not isinstance(record, dict) or not record.get("notification_pending"):
            continue
        status = str(record.get("status") or "")
        if record.get("notified_status") == status:
            record["notification_pending"] = False
            _write_state(state)
            continue
        last = str(record.get("last_notification_attempt_at_utc") or "")
        if last:
            try:
                if now < datetime.fromisoformat(last.replace("Z", "+00:00")) + timedelta(seconds=120):
                    continue
            except ValueError:
                pass
        snap = record.get("notification_candidate")
        if not isinstance(snap, dict):
            failed += 1
            continue
        attempted += 1
        candidate = SimpleNamespace(**snap)
        outcome = _outcome_from_record(str(key), record)
        record["last_notification_attempt_at_utc"] = _iso(now)
        _write_state(state)
        try:
            _notify(candidate, outcome)
        except Exception as exc:
            failed += 1
            record["last_notification_error_type"] = type(exc).__name__
        else:
            sent += 1
            record["notified_status"] = status
            record["notification_pending"] = False
            record.pop("last_notification_error_type", None)
        _write_state(state)
    return {"attempted": attempted, "sent": sent, "failed": failed}


def _cart_has_pnr(client: object, pnr_no: str) -> bool:
    if not pnr_no:
        return False
    response = client.get_cart_list()
    for item in getattr(response, "items", ()) or ():
        if str(getattr(item, "pnr_no", "") or "") == pnr_no:
            return True
    return False


def _seat_class_for_fresh(api: object, fresh: object, target: dict[str, Any]) -> object:
    allowed = target.get("auto_reserve_allowed_cabins", ["general"])
    if isinstance(allowed, str):
        allowed = [allowed]
    allowed_set = {str(x).strip().lower() for x in allowed}
    general_ok = str(getattr(fresh, "general_reservation_code", "") or "").strip() == "11"
    special_ok = str(getattr(fresh, "special_reservation_code", "") or "").strip() == "11"
    if "general" in allowed_set and general_ok:
        return api.KorailSeatClass.GENERAL
    if "special" in allowed_set and special_ok:
        return api.KorailSeatClass.SPECIAL
    raise RuntimeError("fresh query has no designated seat in an allowed cabin")


def _retry_due(record: dict[str, Any]) -> bool:
    raw = str(record.get("next_retry_at_utc") or "")
    if not raw:
        return True
    try:
        return _utc_now() >= datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True


def attempt_auto_reserve_to_cart(
    candidate: object,
    target: dict[str, Any],
    *,
    notify_result: bool = True,
) -> CartReservationOutcome | None:
    """Reserve a designated seat and put the unpaid PNR in KORAIL cart.

    This function never grants payment consent and never calls a payment method.
    """
    if not bool(target.get("auto_reserve_to_cart")):
        return None
    if str(getattr(candidate, "kind", "")) != "DIRECT":
        return None
    if not is_designated_seat_candidate(candidate):
        return None

    key = reservation_key(candidate)
    train_no = str(getattr(candidate, "train_no", "") or "")
    departure_time = _norm_time(getattr(candidate, "departure_time", ""))
    state = _read_state()
    existing = state["records"].get(key)
    if isinstance(existing, dict):
        if existing.get("terminal"):
            return _outcome_from_record(key, existing)
        if not _retry_due(existing):
            return _outcome_from_record(key, existing)

    load_project_env()
    member_no = os.getenv("SEATWATCHER_KORAIL_MEMBER_NO", "").strip()
    password = os.getenv("SEATWATCHER_KORAIL_PASSWORD", "").strip()
    if not member_no or not password:
        return _record_outcome(
            candidate,
            CartReservationOutcome(
                key, "credentials_missing", False, False, False,
                "KORAIL 로그인 정보가 없어 자동예약을 시작하지 못했습니다.",
                train_no, departure_time,
            ),
            terminal=False,
            notify_result=notify_result,
            next_retry_seconds=DEFAULT_RETRY_SECONDS,
        )

    import korail_mobile_api as api

    _patch_train_class_code_validation()
    client = api.KorailClient(api.KorailConfig(enable_dynapath=True))
    stage = "login"
    pnr_no = ""
    try:
        client.login(member_no, password)

        stage = "precheck"
        previous_reservation = _find_history_match(client, candidate)
        if previous_reservation is not None:
            pnr_no = str(getattr(previous_reservation, "pnr_no", "") or "")
            in_cart = _cart_has_pnr(client, pnr_no) if pnr_no else False
            if pnr_no and not in_cart:
                cart_consent = api.MutationConsent(allow_cart=True, dry_run=False)
                cart_response = client.add_to_cart(
                    api.CartAddRequest(pnr_no=pnr_no),
                    consent=cart_consent,
                )
                if str(getattr(cart_response, "str_result", "") or "") != "SUCC":
                    raise RuntimeError("existing reservation cart add did not succeed")
                in_cart = _cart_has_pnr(client, pnr_no)
            outcome = CartReservationOutcome(
                key,
                "cart_confirmed" if in_cart else "already_reserved",
                False,
                bool(pnr_no),
                True,
                "같은 열차의 미결제 예약을 확인했고 장바구니 상태까지 확인했습니다."
                if in_cart else
                "같은 열차의 미결제 예약이 이미 있어 중복 예약을 막았습니다.",
                train_no,
                departure_time,
                True,
                in_cart,
            )
            return _record_outcome(
                candidate, outcome, terminal=True, notify_result=notify_result
            )

        stage = "fresh_search"
        fresh = _find_fresh_train(client, candidate)
        if fresh is None:
            raise RuntimeError("fresh query could not find the same train")
        seat_class = _seat_class_for_fresh(api, fresh, target)

        stage = "reserve"
        reserve_consent = api.MutationConsent(allow_reserve=True, dry_run=False)
        hold = client.reserve(
            fresh,
            consent=reserve_consent,
            passengers=api.KorailPassengerCounts(adult=1),
            seat_class=seat_class,
            job_type=api.KorailReservationJobType.IMMEDIATE,
        )
        if str(getattr(hold, "str_result", "") or "") != "SUCC":
            raise RuntimeError("reservation hold did not succeed")
        pnr_no = str(getattr(hold, "pnr_no", "") or "")
        if not pnr_no:
            raise RuntimeError("reservation hold has no pnr")

        stage = "cart"
        cart_consent = api.MutationConsent(allow_cart=True, dry_run=False)
        cart_response = client.add_to_cart(
            api.CartAddRequest(pnr_no=pnr_no),
            consent=cart_consent,
        )
        if str(getattr(cart_response, "str_result", "") or "") != "SUCC":
            raise RuntimeError("cart add did not succeed")

        stage = "readback"
        history_ok = _find_history_match(client, candidate) is not None
        cart_ok = _cart_has_pnr(client, pnr_no)
        if not history_ok or not cart_ok:
            raise RuntimeError("reservation/cart readback did not confirm")

        return _record_outcome(
            candidate,
            CartReservationOutcome(
                key,
                "cart_confirmed",
                True,
                True,
                True,
                "좌석을 예약하고 장바구니에 담았습니다. 결제는 수행하지 않았습니다.",
                train_no,
                departure_time,
                True,
                True,
            ),
            terminal=True,
            notify_result=notify_result,
        )
    except Exception as exc:
        history_ok = False
        cart_ok = False
        if stage in {"reserve", "cart", "readback"}:
            try:
                history = _find_history_match(client, candidate)
                history_ok = history is not None
                if history is not None:
                    recovered_pnr = str(getattr(history, "pnr_no", "") or "")
                    if recovered_pnr:
                        pnr_no = recovered_pnr
            except Exception:
                history_ok = False
            if pnr_no:
                try:
                    cart_ok = _cart_has_pnr(client, pnr_no)
                except Exception:
                    cart_ok = False

        if history_ok and cart_ok:
            return _record_outcome(
                candidate,
                CartReservationOutcome(
                    key,
                    "cart_confirmed",
                    stage in {"reserve", "cart", "readback"},
                    stage in {"cart", "readback"},
                    True,
                    "예외가 발생했지만 예약내역과 장바구니에서 동일 예약을 확인했습니다. 결제는 수행하지 않았습니다.",
                    train_no,
                    departure_time,
                    True,
                    True,
                ),
                terminal=True,
                notify_result=notify_result,
            )

        if stage in {"reserve", "cart", "readback"} and history_ok:
            return _record_outcome(
                candidate,
                CartReservationOutcome(
                    key,
                    "cart_ambiguous",
                    True,
                    stage in {"cart", "readback"},
                    False,
                    "좌석 예약은 확인됐지만 장바구니 반영을 확정하지 못했습니다. 중복 예약 방지를 위해 자동 재예약하지 않습니다.",
                    train_no,
                    departure_time,
                    True,
                    False,
                ),
                terminal=True,
                notify_result=notify_result,
            )

        if stage in {"reserve", "cart", "readback"}:
            return _record_outcome(
                candidate,
                CartReservationOutcome(
                    key,
                    "reservation_ambiguous",
                    True,
                    stage in {"cart", "readback"},
                    False,
                    f"예약 요청 이후 {type(exc).__name__}이 발생해 성공 여부가 불명확합니다. 자동 재예약을 막았습니다.",
                    train_no,
                    departure_time,
                    False,
                    False,
                ),
                terminal=True,
                notify_result=notify_result,
            )

        return _record_outcome(
            candidate,
            CartReservationOutcome(
                key,
                "preflight_failure",
                False,
                False,
                False,
                f"예약 직전 점검에서 {type(exc).__name__}이 발생했습니다. 좌석이 이미 매진됐을 수 있습니다.",
                train_no,
                departure_time,
                False,
                False,
            ),
            terminal=False,
            notify_result=notify_result,
            next_retry_seconds=DEFAULT_RETRY_SECONDS,
        )
    finally:
        try:
            client.logout()
        except Exception:
            pass
        client.close()
