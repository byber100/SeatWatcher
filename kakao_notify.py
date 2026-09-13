from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from env_loader import load_project_env

AUTHORIZE_URL = "https://kauth.kakao.com/oauth/authorize"
TOKEN_URL = "https://kauth.kakao.com/oauth/token"
SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
TOKEN_SKEW_SECONDS = 300
MAX_TEXT_LENGTH = 200

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_TOKEN_FILE = PROJECT_DIR / ".runtime" / "kakao_tokens.json"


def _env(name: str, *, required: bool = False) -> str:
    value = os.getenv(name, "").strip()
    if required and not value:
        raise RuntimeError(f"환경변수 {name} 값이 필요합니다.")
    return value


def _token_file() -> Path:
    configured = os.getenv("SEATWATCHER_KAKAO_TOKEN_FILE", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_TOKEN_FILE


def _load_tokens() -> dict[str, Any]:
    path = _token_file()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"카카오 토큰 파일을 읽지 못했습니다: {path}") from exc
    return payload if isinstance(payload, dict) else {}


def _save_tokens(tokens: dict[str, Any]) -> None:
    path = _token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(tokens, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _post_form(
    url: str,
    form: dict[str, str],
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            **(headers or {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"카카오 API HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"카카오 API 연결 실패: {exc.reason}") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"카카오 API JSON 해석 실패: {body[:500]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("카카오 API 응답 형식이 예상과 다릅니다.")
    return payload


def build_authorize_url() -> str:
    client_id = _env("SEATWATCHER_KAKAO_REST_API_KEY", required=True)
    redirect_uri = _env("SEATWATCHER_KAKAO_REDIRECT_URI", required=True)
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "talk_message",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def exchange_authorization_code(code: str) -> dict[str, Any]:
    client_id = _env("SEATWATCHER_KAKAO_REST_API_KEY", required=True)
    redirect_uri = _env("SEATWATCHER_KAKAO_REDIRECT_URI", required=True)
    client_secret = _env("SEATWATCHER_KAKAO_CLIENT_SECRET")

    form = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code": code.strip(),
    }
    if client_secret:
        form["client_secret"] = client_secret

    payload = _post_form(TOKEN_URL, form)
    if not payload.get("access_token") or not payload.get("refresh_token"):
        raise RuntimeError(f"카카오 토큰 발급 응답이 불완전합니다: {payload}")

    now = int(time.time())
    payload["access_token_expires_at"] = now + int(payload.get("expires_in") or 0)
    if payload.get("refresh_token_expires_in"):
        payload["refresh_token_expires_at"] = now + int(payload["refresh_token_expires_in"])
    _save_tokens(payload)
    return payload


def refresh_access_token(tokens: dict[str, Any] | None = None) -> dict[str, Any]:
    current = dict(tokens or _load_tokens())
    refresh_token = str(current.get("refresh_token") or "").strip()
    if not refresh_token:
        raise RuntimeError("카카오 refresh_token이 없습니다. 먼저 인가 코드를 발급하세요.")

    client_id = _env("SEATWATCHER_KAKAO_REST_API_KEY", required=True)
    client_secret = _env("SEATWATCHER_KAKAO_CLIENT_SECRET")
    form = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }
    if client_secret:
        form["client_secret"] = client_secret

    payload = _post_form(TOKEN_URL, form)
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError(f"카카오 액세스 토큰 갱신 실패: {payload}")

    now = int(time.time())
    current["access_token"] = access_token
    current["expires_in"] = payload.get("expires_in")
    current["access_token_expires_at"] = now + int(payload.get("expires_in") or 0)

    # 카카오는 만료가 가까운 경우 새 refresh token을 내려줄 수 있다.
    # 이때 기존 refresh token은 폐기될 수 있으므로 반드시 교체 저장한다.
    if payload.get("refresh_token"):
        current["refresh_token"] = payload["refresh_token"]
        current["refresh_token_expires_in"] = payload.get("refresh_token_expires_in")
        if payload.get("refresh_token_expires_in"):
            current["refresh_token_expires_at"] = (
                now + int(payload["refresh_token_expires_in"])
            )

    _save_tokens(current)
    return current


def _usable_access_token() -> str:
    env_token = _env("SEATWATCHER_KAKAO_ACCESS_TOKEN")
    if env_token:
       return env_token

    tokens = _load_tokens()
    access_token = str(tokens.get("access_token") or "").strip()
    expires_at = int(tokens.get("access_token_expires_at") or 0)
    now = int(time.time())

    if access_token and (expires_at == 0 or now + TOKEN_SKEW_SECONDS < expires_at):
        return access_token

    refreshed = refresh_access_token(tokens)
    return str(refreshed["access_token"])


def build_text_template(text: str, link_url: str) -> dict[str, Any]:
    message = text.strip()
    if not message:
        raise ValueError("카카오 륔승지 본원이 비어 있혴.")
    if len(message) > MAX_TEXT_LENGTH:
        message = message[: MAX_TEXT_LENGTH - 1] + "…"
    if not link_url.startswith((h"https://", "http://")):
        raise ValueError("카카오 링크 URL은 http:// 또는 https://로 시작해야 합니다.")

    return {
        "object_type": "text",
        "text": message,
        "link": {
            "web_url": link_url,
            "mobile_web_url": link_url,
        },
        "button_title": "예매 확인",
    }


def send_to_me(text: str, *, link_url: str | None = None) -> dict[str, Any]:
    target_url = (link_url or _env("SEATWATCHER_KAKAO_LINK_URL", required=True)).strip()
    access_token = _usable_access_token()
    template = build_text_template(text, target_url)
    payload = _post_form(
        SEND_URL,
        {"template_object": json.dumps(template, ensure_ascii=False)},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if payload.get("result_code") != 0:
        raise RuntimeError(f"카카오 메시지 발송 실패: {payload}")
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SeatWatcher Kakao 나에게 보내기 설정/테스트")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("authorize-url", help="talk_message 동의용 카카오 로그인 URL 출력")

    exchange = sub.add_parser("exchange-code", help="인가 코드를 토큰으로 교환하고 로컬 저장")
    exchange.add_argument("code")

    send = sub.add_parser("send", help="나와의 채팅으로 테스트 메시지 전송")
    send.add_argument("text")
    return parser.parse_args()


def main() -> int:
    load_project_env()
    args = parse_args()
    if args.command == "authorize-url":
        print(build_authorize_url())
        return 0
    if args.command == "exchange-code":
        payload = exchange_authorization_code(args.code)
        print(
            "카카오 토큰 저장 완료: "
            f"access_expires_in={payload.get('expires_in')}s, "
            f"file={_token_file()}"
        )
        return 0
    if args.command == "send":
        send_to_me(args.text)
        print("카카오 나에게 보내기 성공")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
