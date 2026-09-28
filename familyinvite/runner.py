"""이번 달 발송 계획을 세우고 실행한다.

크론을 **매일** 돌려도 안전하게 만든 것이 핵심이다.
  * send_day 전이면 아무것도 안 한다.
  * 이미 보낸 사람(같은 달·같은 번호)은 건너뛴다.
  * 실패한 사람은 다음 실행에서 다시 시도하고, max_attempts 를 넘으면 포기한다.
그래서 서버가 1일에 꺼져 있었어도 2일 실행에서 따라잡는다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .channels import Channel, ChannelError
from .config import InviteConfig
from .recipients import Recipient
from .store import SENT, Store

log = logging.getLogger(__name__)

PENDING = "pending"
DONE = "done"
GAVE_UP = "gave_up"


def period_of(now: datetime) -> str:
    return f"{now.year:04d}-{now.month:02d}"


def is_due(cfg: InviteConfig, now: datetime) -> bool:
    """이번 달 send_day 일 send_hour 시가 지났는가 (now 는 cfg.tz 기준 시각)."""
    return (now.day, now.hour) >= (cfg.send_day, cfg.send_hour)


def render(cfg: InviteConfig, recipient: Recipient, period: str) -> tuple[str, dict[str, str]]:
    year, month = period.split("-")
    variables = {
        "name": recipient.name,
        "link": recipient.link or cfg.link,
        "month": str(int(month)),
        "year": year,
        "memo": recipient.memo,
    }
    return cfg.message.format_map(variables), variables


def message_kind(text: str) -> str:
    """국내 문자 기준: EUC-KR 90바이트 이하 SMS, 초과 LMS. 요금이 다르다."""
    size = len(text.encode("cp949", errors="replace"))
    return f"SMS {size}B" if size <= 90 else f"LMS {size}B"


@dataclass(slots=True)
class Planned:
    recipient: Recipient
    text: str
    variables: dict[str, str]
    state: str
    attempts: int = 0
    last_error: str = ""


def plan(cfg: InviteConfig, recipients: list[Recipient], store: Store, period: str) -> list[Planned]:
    planned: list[Planned] = []
    for recipient in recipients:
        text, variables = render(cfg, recipient, period)
        record = store.get(period, recipient.phone)
        if record is None:
            state, attempts, error = PENDING, 0, ""
        elif record.status == SENT:
            state, attempts, error = DONE, record.attempts, ""
        elif record.attempts >= cfg.max_attempts:
            state, attempts, error = GAVE_UP, record.attempts, record.error
        else:
            state, attempts, error = PENDING, record.attempts, record.error
        planned.append(Planned(recipient, text, variables, state, attempts, error))
    return planned


def validate_links(cfg: InviteConfig, recipients: list[Recipient]) -> list[str]:
    """템플릿이 {link} 를 쓰는데 링크가 비는 사람 목록."""
    if "{link}" not in cfg.message and not any("{link}" in v for c in cfg.channels.values() for v in (c.get("variables") or {}).values()):
        return []
    return [r.name for r in recipients if not (r.link or cfg.link)]


@dataclass(slots=True)
class Report:
    period: str
    sent: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    already: int = 0
    gave_up: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"📨 {self.period} 발송 결과",
            f"✅ 이번 실행 성공 {len(self.sent)}명 · 이미 발송 {self.already}명",
        ]
        if self.failed:
            lines.append(f"⚠️ 실패 {len(self.failed)}명 (다음 실행에서 재시도)")
            lines += [f"  - {name}: {error[:80]}" for name, error in self.failed]
        if self.gave_up:
            lines.append(f"⛔ 재시도 한도 초과 {len(self.gave_up)}명: {', '.join(self.gave_up)}")
        return "\n".join(lines)


def run(
    planned: list[Planned],
    channels: dict[str, Channel],
    store: Store,
    period: str,
    *,
    gap_seconds: float = 1.0,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> Report:
    report = Report(period)
    first = True
    for item in planned:
        recipient = item.recipient
        if item.state == DONE:
            report.already += 1
            continue
        if item.state == GAVE_UP:
            report.gave_up.append(recipient.name)
            continue

        if dry_run:
            log.info("[dry-run] %s %s via %s", recipient.name, recipient.masked_phone, recipient.channel)
            continue

        channel = channels.get(recipient.channel)
        if channel is None:
            error = f"channels.{recipient.channel} 설정이 없습니다"
            store.record(period, recipient.phone, recipient.name, recipient.channel, ok=False, error=error)
            report.failed.append((recipient.name, error))
            continue

        if not first and gap_seconds > 0:
            sleep(gap_seconds)
        first = False

        try:
            message_id = channel.send(recipient, item.text, item.variables)
        except ChannelError as exc:
            log.warning("발송 실패 %s %s: %s", recipient.name, recipient.masked_phone, exc)
            store.record(period, recipient.phone, recipient.name, recipient.channel, ok=False, error=str(exc))
            report.failed.append((recipient.name, str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001 - 한 사람 실패로 나머지를 멈추지 않는다
            log.exception("예상치 못한 오류 %s", recipient.name)
            store.record(period, recipient.phone, recipient.name, recipient.channel, ok=False, error=repr(exc))
            report.failed.append((recipient.name, repr(exc)))
            continue

        store.record(period, recipient.phone, recipient.name, recipient.channel, ok=True, message_id=message_id)
        report.sent.append(recipient.name)
        log.info("발송 %s %s via %s %s", recipient.name, recipient.masked_phone, channel.name, message_id)
    return report
