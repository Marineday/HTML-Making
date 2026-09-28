"""만료 전 안내 대상 계산과 발송.

규칙
  * reminder_days=[7, 1] 이면 만료 7일 전, 1일 전에 한 번씩 보낸다.
  * 안내 창이 여러 개 열려 있으면 **가장 가까운 것 하나만** 보낸다.
    (만료 하루 전에 등록한 고객에게 D-7 과 D-1 을 동시에 보내지 않는다 — D-7 은 건너뜀 기록)
  * 기록 키는 (고객, 만료일, 며칠 전). 연장해서 만료일이 바뀌면 새 주기로 다시 안내한다.
  * 크론은 매일 돈다. 서버가 하루 꺼져 있었어도 창이 열려 있는 동안 따라잡는다.
  * 나갔는지 모르는 건(타임아웃·5xx)은 자동 재시도하지 않는다.
  * 한 번에 max_per_run 을 넘으면 **아무것도 보내지 않고** 멈춘다 — 날짜 입력 실수로
    전원에게 문자가 나가는 사고를 막는 안전장치다. 확인 후 강제 발송한다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from familyinvite.channels import Channel, ChannelError
from familyinvite.recipients import Recipient

from .config import RenewConfig
from .dates import dday_label, korean
from .store import FAILED, SENT, SKIPPED, UNKNOWN, Customer, Store

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Due:
    customer: Customer
    days_before: int
    days_left: int
    text: str
    #: 동시에 열린 더 먼 안내들. 이번 안내가 나가면 건너뜀으로 기록한다.
    superseded: list[int] = field(default_factory=list)
    attempts: int = 0
    last_error: str = ""


def render(cfg: RenewConfig, customer: Customer, today: date) -> str:
    days_left = (customer.expires_on - today).days
    return cfg.message.format_map({
        "name": customer.name,
        "plan": customer.plan,
        "expires": korean(customer.expires_on),
        "dday": dday_label(customer.expires_on, today),
        "days": str(days_left),
        "link": cfg.link,
        "contact": cfg.contact,
    })


def is_send_time(cfg: RenewConfig, now: datetime) -> bool:
    return now.hour >= cfg.send_hour


def due_reminders(cfg: RenewConfig, store: Store, today: date) -> list[Due]:
    dues: list[Due] = []
    for customer in store.customers(include_inactive=False):
        if customer.sms_opt_out:
            continue
        days_left = (customer.expires_on - today).days
        if days_left < 0:
            continue
        opened = [d for d in cfg.reminder_days if days_left <= d]
        if not opened:
            continue
        target = min(opened)
        record = store.reminder(customer.id, customer.expires_on, target)
        attempts, last_error = 0, ""
        if record is not None:
            if record.status in (SENT, UNKNOWN, SKIPPED):
                continue
            if record.status == FAILED and record.attempts >= cfg.max_attempts:
                continue
            attempts, last_error = record.attempts, record.error
        superseded = [d for d in opened if d != target and store.reminder(customer.id, customer.expires_on, d) is None]
        dues.append(Due(customer, target, days_left, render(cfg, customer, today), superseded, attempts, last_error))
    return dues


@dataclass(slots=True)
class Report:
    sent: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    blocked_by_cap: int = 0

    def summary(self) -> str:
        if self.blocked_by_cap:
            return (f"⛔ 발송 대상 {self.blocked_by_cap}명이 한 번 발송 한도(max_per_run)를 넘어 아무것도 보내지 않았습니다.\n"
                    "   만료일 입력이 잘못되지 않았는지 사이트에서 확인한 뒤 강제 발송하세요.")
        lines = [f"📨 만료 안내: 성공 {len(self.sent)}명"]
        if self.failed:
            lines.append(f"⚠️ 실패 {len(self.failed)}명 (다음 실행에서 재시도)")
            lines += [f"  - {name}: {error[:80]}" for name, error in self.failed]
        if self.unknown:
            lines.append(f"❓ 전송 여부 불명 {len(self.unknown)}명 (자동 재시도 안 함): {', '.join(self.unknown)}")
        return "\n".join(lines)


def to_recipient(customer: Customer) -> Recipient:
    return Recipient(name=customer.name, phone=customer.phone, channel="sms")


def send(
    dues: list[Due],
    channel: Channel,
    store: Store,
    *,
    max_per_run: int,
    gap_seconds: float = 1.0,
    force: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> Report:
    report = Report()
    if len(dues) > max_per_run and not force:
        report.blocked_by_cap = len(dues)
        log.warning("발송 대상 %d명 > max_per_run %d — 중단", len(dues), max_per_run)
        return report

    for index, due in enumerate(dues):
        customer = due.customer
        if index and gap_seconds > 0:
            sleep(gap_seconds)
        try:
            message_id = channel.send(to_recipient(customer), due.text, {})
        except ChannelError as exc:
            status = UNKNOWN if exc.ambiguous else FAILED
            store.record(customer.id, customer.expires_on, due.days_before, status, error=str(exc))
            if exc.ambiguous:
                report.unknown.append(customer.name)
            else:
                report.failed.append((customer.name, str(exc)))
            log.warning("안내 실패 %s %s: %s", customer.name, customer.masked_phone, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - 한 명 때문에 나머지를 멈추지 않는다
            log.exception("예상치 못한 오류 %s", customer.name)
            store.record(customer.id, customer.expires_on, due.days_before, UNKNOWN, error=repr(exc))
            report.unknown.append(customer.name)
            continue

        store.record(customer.id, customer.expires_on, due.days_before, SENT, message_id=message_id)
        for days in due.superseded:
            store.record(customer.id, customer.expires_on, days, SKIPPED, error=f"D-{due.days_before} 안내로 대체", count_attempt=False)
        report.sent.append(customer.name)
        log.info("안내 발송 %s %s D-%d %s", customer.name, customer.masked_phone, due.days_before, message_id)
    return report
