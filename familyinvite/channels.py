"""발송 경로: 문자(솔라피) · 카카오 알림톡(솔라피) · 텔레그램 · 콘솔.

⚠️ 발송 요청은 재시도하지 않는다. 타임아웃이 났어도 상대 서버는 이미 접수했을
수 있어서, 즉시 재시도하면 같은 사람에게 두 통이 나간다. 실패는 기록해 두고
다음 실행(보통 다음 날 크론)에서 다시 시도한다.

솔라피를 고른 이유: 문자와 알림톡을 한 API 키·한 형식으로 보낼 수 있고,
알림톡 실패 시 문자 대체발송도 옵션 하나로 된다. 다른 문자 대행사로 바꾸려면
Channel 을 하나 더 구현하면 된다.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from .recipients import Recipient

SOLAPI_SEND_URL = "https://api.solapi.com/messages/v4/send-many/detail"
TELEGRAM_API = "https://api.telegram.org"
USER_AGENT = "familyinvite/0.1"

#: (url, json body, headers) -> 파싱된 JSON 응답. 테스트에서 가짜로 갈아끼운다.
Transport = Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]


class ChannelError(RuntimeError):
    """발송 실패. 러너는 이걸 잡아 기록하고 다음 사람으로 넘어간다."""


def http_post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float = 15.0) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST")
    request.add_header("Content-Type", "application/json; charset=utf-8")
    request.add_header("User-Agent", USER_AGENT)
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        # 솔라피는 "발신번호 미등록" 같은 이유를 본문에 담아 준다. 버리면 원인을 알 수 없다.
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise ChannelError(f"HTTP {exc.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ChannelError(f"네트워크 오류: {exc}") from None
    try:
        return json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise ChannelError(f"응답 파싱 실패: {exc}") from None


class Channel(Protocol):
    name: str

    def send(self, recipient: Recipient, text: str, variables: dict[str, str]) -> str:
        """보내고 추적용 메시지 ID(없으면 빈 문자열)를 돌려준다. 실패는 ChannelError."""
        ...


def _require(options: dict[str, Any], key: str, where: str) -> str:
    value = options.get(key)
    if value in (None, ""):
        raise ChannelError(f"channels.{where} 설정에 '{key}' 가 필요합니다")
    return str(value)


# ---------------------------------------------------------------------------
# 콘솔 (시험용)
# ---------------------------------------------------------------------------


class ConsoleChannel:
    def __init__(self, name: str, printer: Callable[[str], None] = print) -> None:
        self.name = f"{name}(console)"
        self._print = printer

    def send(self, recipient: Recipient, text: str, variables: dict[str, str]) -> str:
        self._print(f"── [{self.name}] → {recipient.name} {recipient.masked_phone}\n{text}\n")
        return ""


# ---------------------------------------------------------------------------
# 솔라피 (문자 / 알림톡)
# ---------------------------------------------------------------------------


def solapi_auth_header(api_key: str, api_secret: str, *, date: str | None = None, salt: str | None = None) -> str:
    """HMAC-SHA256 apiKey=..., date=..., salt=..., signature=hex(HMAC(secret, date+salt))"""
    date = date or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    salt = salt or secrets.token_hex(16)
    signature = hmac.new(api_secret.encode(), (date + salt).encode(), hashlib.sha256).hexdigest()
    return f"HMAC-SHA256 apiKey={api_key}, date={date}, salt={salt}, signature={signature}"


class _SolapiBase:
    where = "sms"

    def __init__(self, options: dict[str, Any], transport: Transport = http_post_json) -> None:
        self.api_key = _require(options, "api_key", self.where)
        self.api_secret = _require(options, "api_secret", self.where)
        # 발신번호는 솔라피 콘솔에 사전등록된 번호여야 한다(전기통신사업법). 유선번호도 가능.
        self.sender = re.sub(r"\D", "", _require(options, "from", self.where))
        self._post = transport

    def _send_one(self, message: dict[str, Any]) -> str:
        headers = {"Authorization": solapi_auth_header(self.api_key, self.api_secret)}
        try:
            body = self._post(SOLAPI_SEND_URL, {"messages": [message]}, headers)
        except ChannelError as exc:
            # API 키가 오류 메시지로 새지 않게 한다.
            raise ChannelError(str(exc).replace(self.api_secret, "***")) from None

        failed = body.get("failedMessageList") or []
        if failed:
            first = failed[0] if isinstance(failed[0], dict) else {}
            code = first.get("statusCode", "?")
            reason = first.get("statusMessage") or first.get("reason") or json.dumps(first, ensure_ascii=False)[:200]
            raise ChannelError(f"솔라피 접수 거부 [{code}] {reason}")

        for item in body.get("messageList") or []:
            if isinstance(item, dict) and item.get("messageId"):
                return str(item["messageId"])
        group = body.get("groupInfo") or {}
        return str(group.get("groupId") or group.get("_id") or "")


class SolapiSmsChannel(_SolapiBase):
    """단문(SMS)·장문(LMS)은 솔라피가 길이를 보고 자동으로 고른다."""

    name = "sms"
    where = "sms"

    def send(self, recipient: Recipient, text: str, variables: dict[str, str]) -> str:
        return self._send_one({"to": recipient.phone, "from": self.sender, "text": text})


class SolapiAlimtalkChannel(_SolapiBase):
    """카카오 알림톡.

    카카오톡은 전화번호로 임의 메시지를 보낼 공식 방법이 알림톡뿐이다.
    알림톡은 **카카오 비즈니스 채널 + 사전 심사된 템플릿** 이 있어야 하고,
    본문은 템플릿 그대로 나간다(설정의 message 는 쓰지 않음). 바뀌는 부분은
    템플릿 변수(#{이름} 등)로 채운다.
    """

    name = "kakao"
    where = "kakao"

    def __init__(self, options: dict[str, Any], transport: Transport = http_post_json) -> None:
        super().__init__(options, transport)
        self.pf_id = _require(options, "pf_id", "kakao")
        self.template_id = _require(options, "template_id", "kakao")
        self.variable_map: dict[str, str] = dict(options.get("variables") or {})
        # 알림톡 실패(카톡 미사용자 등) 시 같은 내용을 문자로 대체발송할지
        self.sms_fallback = bool(options.get("sms_fallback", True))

    def send(self, recipient: Recipient, text: str, variables: dict[str, str]) -> str:
        filled = {key: template.format_map(variables) for key, template in self.variable_map.items()}
        return self._send_one(
            {
                "to": recipient.phone,
                "from": self.sender,
                "kakaoOptions": {
                    "pfId": self.pf_id,
                    "templateId": self.template_id,
                    "variables": filled,
                    "disableSms": not self.sms_fallback,
                },
            }
        )


# ---------------------------------------------------------------------------
# 텔레그램
# ---------------------------------------------------------------------------


class TelegramChannel:
    """텔레그램 봇은 전화번호로 먼저 말을 걸 수 없다.

    수신자가 봇에게 /start 를 누르고 연락처를 공유해야 번호 ↔ chat_id 가 연결된다
    (`invite.py telegram-link`). 연결 안 된 사람은 실패로 기록된다.
    """

    name = "telegram"

    def __init__(self, options: dict[str, Any], lookup_chat_id: Callable[[str], str | None], transport: Transport = http_post_json) -> None:
        self.token = _require(options, "bot_token", "telegram")
        self._lookup = lookup_chat_id
        self._post = transport

    def send(self, recipient: Recipient, text: str, variables: dict[str, str]) -> str:
        chat_id = self._lookup(recipient.phone)
        if not chat_id:
            raise ChannelError("텔레그램 미연결 — 수신자가 봇에 /start 후 [내 번호 공유] 를 눌러야 합니다")
        return send_telegram(self.token, chat_id, text, self._post)


def send_telegram(token: str, chat_id: str, text: str, transport: Transport = http_post_json, reply_markup: dict[str, Any] | None = None) -> str:
    body: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        body["reply_markup"] = reply_markup
    try:
        result = transport(f"{TELEGRAM_API}/bot{token}/sendMessage", body, {})
    except ChannelError as exc:
        raise ChannelError(str(exc).replace(token, "***")) from None
    if not result.get("ok"):
        raise ChannelError(f"텔레그램 API 오류: {result.get('description', result)}")
    return str((result.get("result") or {}).get("message_id", ""))


# ---------------------------------------------------------------------------


def build(channel: str, options: dict[str, Any], *, lookup_chat_id: Callable[[str], str | None], transport: Transport = http_post_json) -> Channel:
    provider = str(options.get("provider", "console" if channel != "telegram" else "telegram")).lower()
    if provider == "console":
        return ConsoleChannel(channel)
    if channel == "sms" and provider == "solapi":
        return SolapiSmsChannel(options, transport)
    if channel == "kakao" and provider == "solapi":
        return SolapiAlimtalkChannel(options, transport)
    if channel == "telegram" and provider == "telegram":
        return TelegramChannel(options, lookup_chat_id, transport)
    raise ChannelError(f"channels.{channel}.provider 에 '{provider}' 는 쓸 수 없습니다")
