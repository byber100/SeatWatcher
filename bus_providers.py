from __future__ import annotations

import html
import http.cookiejar
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BusCandidate:
    provider: str
    departure_terminal: str
    arrival_terminal: str
    date: str
    departure_time: str
    company: str | None = None
    bus_class: str | None = None
    remaining_seats: int | None = None
    total_seats: int | None = None
    schedule_type: str | None = None
    bookable: bool = True


KOBUS_TERMINALS = {
    "서울경부": "010",
    "동서울": "032",
    "인천": "100",
    "동대구": "801",
}

TMONEY_TERMINALS = {
    "성남": ("1349701", "성남"),
    "동대구": ("4124601", "동대구"),
    "수원터미널": ("1658501", "수원터미널"),
    "서수원": ("1640501", "서수원"),
}

BUSTAGO_TERMINALS = {
    "성남종합": "1010",
    "수원터미널": "1012",
    "인천": "9302",
    "동대구": "9201",
    "경산": "5005",
}

_KOBUS_CALL_RE = re.compile(r"fnSatsChc\((.*?)\)", re.S)
_TMONEY_CALL_RE = re.compile(r"readSasFeeInf\((.*?)\)", re.S)
_QUOTED_ARG_RE = re.compile(r"'((?:\\'|[^'])*)'")
_BUSTAGO_ENDPOINT_RE = re.compile(
    r"url\s*:\s*['\"]([^'\"]*ticketListJson3\.do(?:;jsessionid=[^'\"]+)?)['\"]"
)


def _normalize_hhmm(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 4:
        return digits[:4]
    return digits.zfill(4)


def _in_window(hhmm: str, start_hhmm: str, end_hhmm: str) -> bool:
    return start_hhmm <= hhmm <= end_hhmm


def _to_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _curl_binary() -> str:
    for name in ("curl.exe", "curl"):
        path = shutil.which(name)
        if path:
            return path
    raise RuntimeError("웹 조회에 사용할 curl 실행 파일을 찾을 수 없습니다.")


def _run_curl(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_curl_binary(), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def search_kobus(
    departure_terminal: str,
    arrival_terminal: str,
    date: str,
    start_hhmm: str,
    end_hhmm: str,
    *,
    include_unavailable: bool = False,
) -> list[BusCandidate]:
    departure_code = KOBUS_TERMINALS[departure_terminal]
    arrival_code = KOBUS_TERMINALS[arrival_terminal]
    base = "https://www.kobus.co.kr"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        cookie = temp / "cookie.txt"
        main_html = temp / "main.html"
        result_html = temp / "result.html"

        initial = _run_curl([
            "-sS", "--max-time", "20", "-A", "Mozilla/5.0",
            "-c", str(cookie), "-b", str(cookie), "-o", str(main_html),
            f"{base}/main.do",
        ])
        if initial.returncode != 0:
            raise RuntimeError(f"KOBUS 초기 페이지 조회 실패: {initial.stderr.strip()}")

        form = urllib.parse.urlencode({
            "deprCd": departure_code,
            "arvlCd": arrival_code,
            "pathDvs": "sngl",
            "pathStep": "1",
            "deprDtm": date,
            "busClsCd": "0",
            "rtrpChc": "1",
            "timeLinkMin": start_hhmm[:2],
            "timeLinkMax": end_hhmm[:2],
        })
        search = _run_curl([
            "-sS", "--max-time", "20", "-A", "Mozilla/5.0",
            "-c", str(cookie), "-b", str(cookie),
            "-H", f"Referer: {base}/main.do",
            "-H", "Content-Type: application/x-www-form-urlencoded",
            "-X", "POST", "--data", form, "-o", str(result_html),
            f"{base}/mrs/alcnSrch.do",
        ])
        if search.returncode != 0:
            raise RuntimeError(f"KOBUS 배차 조회 실패: {search.stderr.strip()}")
        body = result_html.read_text(encoding="utf-8", errors="replace")

    row_re = re.compile(r'<p\b[^>]*role="row"[^>]*>(.*?)</p>', re.S | re.I)

    def cell_text(row: str, class_name: str) -> str:
        match = re.search(
            rf'class="{re.escape(class_name)}"[^>]*>(.*?)</span>',
            row,
            re.S | re.I,
        )
        if not match:
            return ""
        value = re.sub(r"<[^>]+>", " ", match.group(1))
        return re.sub(r"\s+", " ", html.unescape(value)).strip()

    candidates: list[BusCandidate] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in row_re.findall(body):
        row_text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", row))).strip()
        call = _KOBUS_CALL_RE.search(row)
        args = (
            [value.replace("\\'", "'") for value in _QUOTED_ARG_RE.findall(call.group(1))]
            if call
            else []
        )

        departure_time = ""
        if len(args) >= 2 and len(args[1]) >= 4:
            departure_time = _normalize_hhmm(args[1])
        else:
            time_match = re.search(r"\b(\d{1,2})\s*:\s*(\d{2})\b", row_text)
            if time_match:
                departure_time = f"{int(time_match.group(1)):02d}{time_match.group(2)}"
        if not departure_time or not _in_window(departure_time, start_hhmm, end_hhmm):
            continue

        company = cell_text(row, "bus_com") or None
        bus_class = None
        for label in ("프리미엄", "심야우등", "우등", "고속"):
            if label in row_text:
                bus_class = label
                break

        # KOBUS 웹은 명절 추가 배차를 등급 옆 '(임시)'로 표시한다.
        # 사용자가 말하는 정규외 좌석으로 정규화해 일반 배차와 동일하게 감시한다.
        schedule_type = "정규외" if ("정규외" in row_text or "임시" in row_text) else None

        remain_text = cell_text(row, "remain")
        remain_match = re.search(r"\d+", remain_text.replace(",", ""))
        remaining = int(remain_match.group(0)) if remain_match else None
        bookable = call is not None and (remaining is None or remaining > 0)
        if not include_unavailable and not bookable:
            continue

        key = (departure_time, company or "", bus_class or "", schedule_type or "")
        if key in seen:
            continue
        seen.add(key)

        candidates.append(
            BusCandidate(
                provider="KOBUS",
                departure_terminal=departure_terminal,
                arrival_terminal=arrival_terminal,
                date=date,
                departure_time=departure_time,
                company=company,
                bus_class=bus_class,
                remaining_seats=remaining,
                schedule_type=schedule_type,
                bookable=bookable,
            )
        )

    return candidates


def search_tmoney(
    departure_terminal: str,
    arrival_terminal: str,
    date: str,
    start_hhmm: str,
    end_hhmm: str,
    *,
    include_unavailable: bool = False,
) -> list[BusCandidate]:
    departure_code, departure_name = TMONEY_TERMINALS[departure_terminal]
    arrival_code, arrival_name = TMONEY_TERMINALS[arrival_terminal]
    base = "https://intercitybus.tmoney.co.kr"
    entry_url = f"{base}/otck/trmlInfEnty.do"
    search_url = f"{base}/otck/readAlcnList.do"

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    headers = {"User-Agent": "Mozilla/5.0"}
    with opener.open(urllib.request.Request(entry_url, headers=headers), timeout=20) as response:
        response.read()

    form = urllib.parse.urlencode(
        {
            "depr_Trml_Cd": departure_code,
            "arvl_Trml_Cd": arrival_code,
            "depr_Trml_Nm": departure_name,
            "arvl_Trml_Nm": arrival_name,
            "ig": "1",
            "im": "0",
            "ic": "0",
            "iv": "0",
            "depr_Dt": date,
            "depr_Time": start_hhmm + "00",
            "bef_Aft_Dvs": "D",
            "req_Rec_Num": "10",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        search_url,
        data=form,
        headers={
            **headers,
            "Referer": entry_url,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with opener.open(request, timeout=20) as response:
        body = response.read().decode(
            response.headers.get_content_charset() or "utf-8",
            errors="replace",
        )

    candidates: list[BusCandidate] = []
    seen: set{tuple[str, str | None]} = set()
    for match in _TMONEY_CALL_RE.finditer(body):
        args = [value.replace("\\'", "'") for value in _QUOTED_ARG_RE.findall(match.group(1))]
        if len(args) < 18:
            continue

        departure_time = _normalize_hhmm(args[8])
        if not _in_window(departure_time, start_hhmm, end_hhmm):
            continue

        company = args[11] or None
        key = (departure_time, company)
        if key in seen:
            continue
        seen.add(key)

        remaining = _to_int(args[16])
        total = _to_int(args[17])
        bookable = remaining is None or remaining > 0
        if not include_unavailable and not bookable:
            continue

        candidates.append(
            BusCandidate(
                provider="TMONEY_INTERCITY",
                departure_terminal=departure_terminal,
                arrival_terminal=arrival_terminal,
                date=date,
                departure_time=departure_time,
                company=company,
                bus_class=args[12] or None,
                remaining_seats=remaining,
                total_seats=total,
                bookable=bookable,
            )
        )

    return candidates


def search_bustago(
    departure_terminal: str,
    arrival_terminal: str,
    date: str,
    start_hhmm: str,
    end_hhmm: str,
    *,
    include_unavailable: bool = False,
) -> list[BusCandidate]:
    departure_id = BUSTAGO_TERMINALS[departure_terminal]
    arrival_id = BUSTAGO_TERMINALS[arrival_terminal]
    base = "https://www.bustago.or.kr"
    page_url = f"{base}/newweb/kr/ticket/ticket.do"

    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        cookie = temp / "cookie.txt"
        page_file = temp / "ticket.html"
        result_file = temp / "result.json"

        page_result = _run_curl(
            [
                "-sS",
                "--max-time",
                "20",
                "-A",
                "Mozilla/5.0",
                "-c",
                str(cookie),
                "-b",
                str(cookie),
                "-o",
                str(page_file),
                page_url,
            ]
        )
        if page_result.returncode != 0:
            raise RuntimeError(f"버스타고 초기 페이지 조회 실패: {page_result.stderr.strip()}")

        page_text = page_file.read_text(encoding="utf-8", errors="replace")
        endpoint_match = _BUSTAGO_ENDPOINT_RE.search(page_text)
        if endpoint_match is None:
            raise RuntimeError("버스타고 배차 JSON 엔드포인트를 찾지 못했습니다.")
        endpoint = endpoint_match.group(1)
        if not endpoint.startswith("http"):
            endpoint = base + endpoint

        form = urllib.parse.urlencode(
            {
                "startType": "S",
                "orderDate": date,
                "orderBackDate": date,
                "depTerId": departure_id,
                "arrTerId": arrival_id,
                "depTime": start_hhmm,
                "arrTime": "0000",
                "goBusGrade": "0",
                "goBackBusGrade": "0",
            }
        )
        result = _run_curl(
            [
                "-sS",
                "--max-time",
                "20",
                "-A",
                "Mozilla/5.0",
                "-c",
                str(cookie),
                "-b",
                str(cookie),
                "-H",
                f"Referer: {page_url}",
                "-H",
                "Content-Type: application/x-www-form-urlencoded",
                "-X",
                "POST",
                "--data",
                form,
                "-o",
                str(result_file),
                endpoint,
            ]
        )
        if result.returncode != 0:
            raise RuntimeError(f"버스타고 배차 조회 실패: {result.stderr.strip()}")

        try:
            payload = json.loads(result_file.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("버스타고 배차 응답 JSON 해석 실패") from exc

    candidates: list[BusCandidate] = []
    seen: set[tuple[str, str | None]] = set()
    for row in payload.get("ticketSingleList") or []:
        departure_time = _normalize_hhmm(str(row.get("DEP_TIME") or ""))
        if not _in_window(departure_time, start_hhmm, end_hhmm):
            continue

        remaining = _to_int(row.get("REMAIN_CNT"))
        total = _to_int(row.get("TOT_SEAT_CNT"))
        open_gbn = str(row.get("OPENGBN") or "")
        reservation_check = str(row.get("RESERVATION_CHK") or "")
        bookable = (
            open_gbn != "1"
            and reservation_check != "0"
            and (total is None or total > 0)
            and (remaining is None or remaining > 0)
        )
        if not include_unavailable and not bookable:
            continue

        company = str(row.get("TRANSP_BIZR_NM") or "").strip() or None
        key = (departure_time, company)
        if key in seen:
            continue
        seen.add(key)

        candidates.append(
            BusCandidate(
                provider="BUSTAGO",
                departure_terminal=departure_terminal,
                arrival_terminal=arrival_terminal,
                date=date,
                departure_time=departure_time,
                company=company,
                bus_class=str(row.get("BUS_GRADE_NM") or "").strip() or None,
                remaining_seats=remaining,
                total_seats=total,
                bookable=bookable,
            )
        )

    return candidates


def search_user_bus_routes(
    departure_city: str,
    arrival_city: str,
    date: str,
    start_hhmm: str,
    end_hhmm: str,
    *,
    include_unavailable: bool = False,
) -> list[BusCandidate]:
    routes: list[tuple[str, str, str]] = []

    if (departure_city, arrival_city) == ("서울", "동대구"):
        routes.extend(
            [
                ("kobus", "서울경부", "동대구"),
                ("kobus", "동서울", "동대구"),
            ]
        )
    elif (departure_city, arrival_city) == ("동대구", "서울"):
        routes.extend(
            [
                ("kobus", "동대구", "서울경부"),
                ("kobus", "동대구", "동서울"),
            ]
        )
    elif (departure_city, arrival_city) == ("인천", "동대구"):
        routes.append(("kobus", "인천", "동대구"))
    elif (departure_city, arrival_city) == ("동대구", "인천"):
        routes.append(("kobus", "동대구", "인천"))
    elif (departure_city, arrival_city) == ("성남", "동대구"):
        routes.extend(
            [
                ("bustago", "성남종합", "동대구"),
                ("tmoney", "성남", "동대구"),
            ]
        )
    elif (departure_city, arrival_city) == ("동대구", "성남"):
        routes.extend(
            [
                ("bustago", "동대구", "성남종합"),
                ("tmoney", "동대구", "성남"),
            ]
        )
    elif (departure_city, arrival_city) == ("수원", "동대구"):
        routes.extend(
            [
                ("bustago", "수원터미널", "동대구"),
                ("tmoney", "수원터미널", "동대구"),
                ("tmoney", "서수원", "동대구"),
            ]
        )
    elif (departure_city, arrival_city) == ("동대구", "수원"):
        routes.extend(
            [
                ("bustago", "동대구", "수원터미널"),
                ("tmoney", "동대구", "수원터미널"),
                ("tmoney", "동대구", "서수원"),
            ]
        )

    results: list[BusCandidate] = []
    for provider, departure_terminal, arrival_terminal in routes:
        try:
            if provider == "kobus":
                candidates = search_kobus(
                    departure_terminal,
                    arrival_terminal,
                    date,
                    start_hhmm,
                    end_hhmm,
                    include_unavailable=include_unavailable,
                )
            elif provider == "tmoney":
                candidates = search_tmoney(
                    departure_terminal,
                    arrival_terminal,
                    date,
                    start_hhmm,
                    end_hhmm,
                    include_unavailable=include_unavailable,
                )
            else:
                candidates = search_bustago(
                    departure_terminal,
                    arrival_terminal,
                    date,
                    start_hhmm,
                    end_hhmm,
                    include_unavailable=include_unavailable,
                )
            results.extend(candidates)
        except Exception as exc:
            print(
                f"WARNING: {provider} {departure_terminal}->{arrival_terminal} 조회 실패: {exc}",
                file=sys.stderr,
            )

    return sorted(
        results,
        key=lambda item: (
            item.departure_time,
            item.provider,
            item.departure_terminal,
            item.arrival_terminal,
        ),
    )
