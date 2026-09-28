"""invite.json 설정 로드와 검증.

핫딜 봇 설정과 같은 규칙을 쓴다: `//` 줄 주석 허용, 토큰은 ${ENV_VAR} 로 참조,
참조한 환경변수가 없으면 즉시 실패.
"""

from __future__ import annotations

import json
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hotdeal.config import ConfigError, _expand_env, strip_comments

from .recipients import CHANNELS

#: 메시지 템플릿에서 쓸 수 있는 자리표시자
PLACEHOLDERS = ("name", "link", "link_noscheme", "month", "year", "memo")


@dataclass(slots=True)
class InviteConfig:
    message: str
    link: str = ""
    recipients_path: str = "recipients.csv"
    db_path: str = "invite.db"
    timezone: str = "Asia/Seoul"
    #: 매월 이 날짜 send_hour 시 이후 첫 실행에서 발송한다 (1~28).
    send_day: int = 1
    send_hour: int = 10
    default_channel: str = "sms"
    #: true 면 그달 발송 전에 `invite.py approve` 로 사람이 승인해야 한다.
    approval_required: bool = True
    #: 한 사람에게 한 달에 시도하는 최대 횟수. 넘으면 그달은 포기하고 보고만 한다.
    max_attempts: int = 3
    send_gap_seconds: float = 1.0
    #: 승인 요청·결과 보고를 받을 운영자 텔레그램 chat_id (선택)
    owner_telegram_chat_id: str = ""
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)
    log_level: str = "INFO"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load(path: str | Path) -> InviteConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"설정 파일이 없습니다: {path}. invite.example.json 을 복사해서 만드세요.")
    try:
        raw = json.loads(strip_comments(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} JSON 파싱 실패: {exc}") from exc
    return from_dict(_expand_env(raw))


def from_dict(raw: dict[str, Any]) -> InviteConfig:
    if not isinstance(raw, dict):
        raise ConfigError("설정 최상위는 객체(JSON object)여야 합니다")

    known = set(InviteConfig.__dataclass_fields__) | {"owner"}  # type: ignore[attr-defined]
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"알 수 없는 설정 키: {sorted(unknown)}")

    message = raw.get("message", "")
    if not message:
        raise ConfigError("message(보낼 문구 템플릿)가 필요합니다")
    check_template(message, "message")

    owner = raw.get("owner") or {}
    channels = raw.get("channels") or {}
    if not isinstance(channels, dict) or set(channels) - set(CHANNELS):
        raise ConfigError(f"channels 의 키는 {CHANNELS} 중에서만 쓸 수 있습니다")

    cfg = InviteConfig(
        message=message,
        link=str(raw.get("link", "")),
        recipients_path=str(raw.get("recipients_path", "recipients.csv")),
        db_path=str(raw.get("db_path", "invite.db")),
        timezone=str(raw.get("timezone", "Asia/Seoul")),
        send_day=int(raw.get("send_day", 1)),
        send_hour=int(raw.get("send_hour", 10)),
        default_channel=str(raw.get("default_channel", "sms")).lower(),
        approval_required=bool(raw.get("approval_required", True)),
        max_attempts=int(raw.get("max_attempts", 3)),
        send_gap_seconds=float(raw.get("send_gap_seconds", 1.0)),
        owner_telegram_chat_id=str(owner.get("telegram_chat_id", "") if isinstance(owner, dict) else ""),
        channels=channels,
        log_level=str(raw.get("log_level", "INFO")).upper(),
    )

    # 29~31일은 없는 달이 있어 그달 발송이 통째로 빠진다.
    if not 1 <= cfg.send_day <= 28:
        raise ConfigError("send_day 는 1~28 사이여야 합니다 (29~31일은 2월 등에 존재하지 않음)")
    if not 0 <= cfg.send_hour <= 23:
        raise ConfigError("send_hour 는 0~23 사이여야 합니다")
    if cfg.default_channel not in CHANNELS:
        raise ConfigError(f"default_channel 은 {CHANNELS} 중 하나여야 합니다")
    if cfg.max_attempts < 1:
        raise ConfigError("max_attempts 는 1 이상이어야 합니다")
    if cfg.link and not cfg.link.startswith(("https://", "http://")):
        raise ConfigError("link 는 http(s):// 로 시작해야 합니다")
    try:
        ZoneInfo(cfg.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"timezone 을 알 수 없습니다: {cfg.timezone}") from exc
    return cfg


def check_template(template: str, where: str) -> None:
    """모르는 자리표시자나 짝이 안 맞는 중괄호를 설정 로드 시점에 잡는다."""
    check_template_fields(template, where, PLACEHOLDERS)


def check_template_fields(template: str, where: str, allowed: tuple[str, ...]) -> None:
    try:
        fields = [name for _, name, _, _ in string.Formatter().parse(template) if name is not None]
    except ValueError as exc:
        raise ConfigError(f"{where} 템플릿의 중괄호가 잘못됐습니다: {exc}. 글자 그대로의 {{ 는 {{{{ 로 쓰세요.") from exc
    for name in fields:
        if name not in allowed:
            raise ConfigError(f"{where} 템플릿의 {{{name}}} 는 쓸 수 없습니다. 가능한 것: {allowed}")
