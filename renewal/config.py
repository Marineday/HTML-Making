"""renew.json 설정. familyinvite·핫딜 봇과 같은 규칙(// 주석, ${ENV} 치환)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from familyinvite.config import check_template_fields
from hotdeal.config import ConfigError, _expand_env, strip_comments

#: 안내 문구에서 쓸 수 있는 자리표시자
PLACEHOLDERS = ("name", "plan", "expires", "dday", "days", "link", "contact")
MIN_PASSWORD = 12


@dataclass(slots=True)
class RenewConfig:
    message: str
    db_path: str = "renewal.db"
    timezone: str = "Asia/Seoul"
    #: 만료 며칠 전에 안내할지. [7, 1] 이면 D-7, D-1 두 번. 0 은 만료 당일.
    reminder_days: list[int] = field(default_factory=lambda: [7, 1])
    #: 이 시각 이전 실행에서는 보내지 않는다 (새벽 문자 방지)
    send_hour: int = 10
    #: false 면 자동 발송하지 않고 사이트의 [발송] 버튼으로만 보낸다
    auto_send: bool = True
    #: 한 번 실행에서 보낼 최대 건수. 날짜 버그로 전원에게 쏘는 사고를 막는 안전장치.
    max_per_run: int = 50
    max_attempts: int = 3
    send_gap_seconds: float = 1.0
    link: str = ""
    contact: str = ""
    sms: dict[str, Any] = field(default_factory=lambda: {"provider": "console"})
    admin_password: str = ""
    admin_host: str = "127.0.0.1"
    admin_port: int = 8090
    #: 세션 서명 키. 비우면 서버 시작 때 무작위 생성 → 재시작하면 다시 로그인.
    secret_key: str = ""
    log_level: str = "INFO"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load(path: str | Path) -> RenewConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"설정 파일이 없습니다: {path}. renew.example.json 을 복사해서 만드세요.")
    try:
        raw = json.loads(strip_comments(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} JSON 파싱 실패: {exc}") from exc
    return from_dict(_expand_env(raw))


def from_dict(raw: dict[str, Any]) -> RenewConfig:
    if not isinstance(raw, dict):
        raise ConfigError("설정 최상위는 객체여야 합니다")
    known = {"message", "db_path", "timezone", "reminder_days", "send_hour", "auto_send", "max_per_run",
             "max_attempts", "send_gap_seconds", "link", "contact", "sms", "admin", "log_level"}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"알 수 없는 설정 키: {sorted(unknown)}")

    message = str(raw.get("message", ""))
    if not message:
        raise ConfigError("message(안내 문구 템플릿)가 필요합니다")
    check_template_fields(message, "message", PLACEHOLDERS)

    admin = raw.get("admin") or {}
    if not isinstance(admin, dict):
        raise ConfigError("admin 은 객체여야 합니다")
    days = raw.get("reminder_days", [7, 1])
    if not isinstance(days, list) or not days or not all(isinstance(d, int) and 0 <= d <= 60 for d in days):
        raise ConfigError("reminder_days 는 0~60 사이 정수 목록이어야 합니다 (예: [7, 1])")

    cfg = RenewConfig(
        message=message,
        db_path=str(raw.get("db_path", "renewal.db")),
        timezone=str(raw.get("timezone", "Asia/Seoul")),
        reminder_days=sorted(set(days), reverse=True),
        send_hour=int(raw.get("send_hour", 10)),
        auto_send=bool(raw.get("auto_send", True)),
        max_per_run=int(raw.get("max_per_run", 50)),
        max_attempts=int(raw.get("max_attempts", 3)),
        send_gap_seconds=float(raw.get("send_gap_seconds", 1.0)),
        link=str(raw.get("link", "")),
        contact=str(raw.get("contact", "")),
        sms=dict(raw.get("sms") or {"provider": "console"}),
        admin_password=str(admin.get("password", "")),
        admin_host=str(admin.get("host", "127.0.0.1")),
        admin_port=int(admin.get("port", 8090)),
        secret_key=str(admin.get("secret_key", "")),
        log_level=str(raw.get("log_level", "INFO")).upper(),
    )
    if not 0 <= cfg.send_hour <= 23:
        raise ConfigError("send_hour 는 0~23 이어야 합니다")
    if cfg.max_per_run < 1 or cfg.max_attempts < 1:
        raise ConfigError("max_per_run, max_attempts 는 1 이상이어야 합니다")
    try:
        ZoneInfo(cfg.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"timezone 을 알 수 없습니다: {cfg.timezone}") from exc
    return cfg


def require_admin_password(cfg: RenewConfig) -> None:
    """사이트를 띄울 때만 검사한다 (문자 발송 크론에는 필요 없음)."""
    if len(cfg.admin_password) < MIN_PASSWORD:
        raise ConfigError(f"admin.password 는 {MIN_PASSWORD}자 이상이어야 합니다. 환경변수로 넣으세요: \"password\": \"${{RENEW_ADMIN_PASSWORD}}\"")
