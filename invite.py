#!/usr/bin/env python3
"""매월 링크 자동 발송 CLI.

  python3 invite.py check              # 설정·명단 검증, 수신자별 경로와 문자 길이 표
  python3 invite.py preview            # 이번 달 나갈 메시지 전부 출력 (발송 안 함)
  python3 invite.py approve            # 이번 달 발송 승인 (approval_required 일 때)
  python3 invite.py send               # 크론용. 발송일 전·미승인이면 아무것도 안 보냄
  python3 invite.py send --dry-run     # 대상만 출력
  python3 invite.py status             # 이번 달 누가 받았고 누가 실패했는지
  python3 invite.py reset --phone ...  # 특정인 기록을 지워 재발송 가능하게
  python3 invite.py telegram-link      # 텔레그램 수신자 번호 연결 대기 (상시 실행)
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import datetime

from familyinvite import channels as channels_module
from familyinvite import config as config_module
from familyinvite import recipients as recipients_module
from familyinvite import runner, telegram_link
from familyinvite.channels import ChannelError
from familyinvite.config import ConfigError, InviteConfig, check_template
from familyinvite.recipients import Recipient, RecipientError
from familyinvite.store import Store

log = logging.getLogger("invite")
_PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%m-%d %H:%M:%S")


def _load(args: argparse.Namespace) -> tuple[InviteConfig, list[Recipient], Store]:
    cfg = config_module.load(args.config)
    _setup_logging(args.log_level or cfg.log_level)
    recipients = recipients_module.load(cfg.recipients_path, cfg.default_channel)
    if not recipients:
        raise RecipientError(f"{cfg.recipients_path} 에 발송 대상이 없습니다")
    missing = runner.validate_links(cfg, recipients)
    if missing:
        raise ConfigError(f"링크가 비어 있는 수신자: {', '.join(missing)} — 설정의 link 또는 명단의 link 열을 채우세요")
    return cfg, recipients, Store(cfg.db_path)


def _now(cfg: InviteConfig) -> datetime:
    return datetime.now(cfg.tz)


def _period(args: argparse.Namespace, cfg: InviteConfig) -> str:
    period = getattr(args, "period", None) or runner.period_of(_now(cfg))
    if not _PERIOD.match(period):
        raise ConfigError(f"--period 는 YYYY-MM 형식이어야 합니다 (받은 값: {period})")
    return period


def _build_channels(cfg: InviteConfig, recipients: list[Recipient], store: Store) -> dict[str, channels_module.Channel]:
    """명단에서 실제로 쓰는 경로만 만든다. 설정이 빠졌으면 여기서 바로 실패한다."""
    built: dict[str, channels_module.Channel] = {}
    for name in sorted({r.channel for r in recipients}):
        options = cfg.channels.get(name)
        if options is None:
            raise ConfigError(f"명단에 {name} 수신자가 있는데 channels.{name} 설정이 없습니다")
        for key, template in (options.get("variables") or {}).items():
            check_template(str(template), f"channels.{name}.variables[{key}]")
        built[name] = channels_module.build(name, options, lookup_chat_id=store.telegram_chat_id)
    return built


def _notify_owner(cfg: InviteConfig, text: str) -> None:
    """운영자에게 텔레그램으로 알린다. 설정이 없으면 로그로만 남긴다."""
    token = (cfg.channels.get("telegram") or {}).get("bot_token", "")
    if not (cfg.owner_telegram_chat_id and token):
        log.info("운영자 알림 (owner.telegram_chat_id 미설정 — 로그로만):\n%s", text)
        return
    try:
        channels_module.send_telegram(token, cfg.owner_telegram_chat_id, text)
    except ChannelError as exc:
        log.warning("운영자 알림 실패: %s", exc)


# ---------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    cfg, recipients, store = _load(args)
    try:
        _build_channels(cfg, recipients, store)
        period = _period(args, cfg)
        print(f"\n발송 예정: 매월 {cfg.send_day}일 {cfg.send_hour}시 이후 ({cfg.timezone}) · 승인 {'필요' if cfg.approval_required else '불필요'}")
        print(f"{'이름':<10} {'번호':<14} {'경로':<9} 비고")
        print("-" * 60)
        for item in runner.plan(cfg, recipients, store, period):
            r = item.recipient
            if r.channel == "telegram" and (cfg.channels.get("telegram") or {}).get("provider", "telegram") == "telegram":
                note = "연결됨" if store.telegram_chat_id(r.phone) else "⚠️ 미연결 (telegram-link 필요)"
            elif r.channel == "sms":
                note = runner.message_kind(item.text)
            else:
                note = "알림톡 템플릿"
            print(f"{r.name:<10} {r.masked_phone:<14} {r.channel:<9} {note}")
        print(f"\n총 {len(recipients)}명 — 설정 OK\n")
    finally:
        store.close()
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    cfg, recipients, store = _load(args)
    try:
        period = _period(args, cfg)
        for item in runner.plan(cfg, recipients, store, period):
            r = item.recipient
            state = {"done": "이미 발송", "gave_up": "재시도 한도 초과", "pending": "발송 예정"}[item.state]
            print(f"── {r.name} {r.masked_phone} [{r.channel}] {state}")
            if r.channel == "kakao":
                variables = (cfg.channels.get("kakao") or {}).get("variables") or {}
                print("   (알림톡 — 템플릿 변수) " + ", ".join(f"{k}={v.format_map(item.variables)}" for k, v in variables.items()))
            else:
                print(item.text)
            print()
    finally:
        store.close()
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    cfg, recipients, store = _load(args)
    try:
        period = _period(args, cfg)
        store.approve(period)
        print(f"{period} 발송 승인 완료 — 다음 `send` 실행에서 {len(recipients)}명에게 발송됩니다.")
    finally:
        store.close()
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    cfg, recipients, store = _load(args)
    try:
        now = _now(cfg)
        period = _period(args, cfg)
        if not args.period and not args.now and not runner.is_due(cfg, now):
            log.info("아직 발송일 전입니다 (매월 %d일 %d시). 종료.", cfg.send_day, cfg.send_hour)
            return 0

        planned = runner.plan(cfg, recipients, store, period)
        pending = [p for p in planned if p.state == runner.PENDING]
        if not pending:
            log.info("%s 발송할 사람이 없습니다 (전원 발송 완료 또는 재시도 한도 초과).", period)
            return 0

        if cfg.approval_required and not store.is_approved(period) and not args.dry_run:
            if not store.was_notified(period):
                names = ", ".join(p.recipient.name for p in pending)
                _notify_owner(
                    cfg,
                    f"🔔 {period} 발송 승인 대기\n대상 {len(pending)}명: {names}\n\n"
                    f"미리보기: python3 invite.py preview --period {period}\n"
                    f"승인:     python3 invite.py approve --period {period}",
                )
                store.mark_notified(period)
            log.info("%s 은 아직 승인되지 않았습니다. `invite.py approve --period %s` 후 다음 실행에서 발송됩니다.", period, period)
            return 0

        channels = {} if args.dry_run else _build_channels(cfg, recipients, store)
        report = runner.run(planned, channels, store, period, gap_seconds=cfg.send_gap_seconds, dry_run=args.dry_run)
        if args.dry_run:
            print(f"[dry-run] {period} 발송 대상 {len(pending)}명 — 실제로는 보내지 않았습니다.")
            return 0
        print(report.summary())
        if report.sent or report.failed:
            _notify_owner(cfg, report.summary())
        return 1 if report.failed and not report.sent else 0
    finally:
        store.close()


def cmd_status(args: argparse.Namespace) -> int:
    cfg, recipients, store = _load(args)
    try:
        period = _period(args, cfg)
        print(f"\n{period} · 승인 {'✅' if store.is_approved(period) else '⏳ 대기'}")
        labels = {"done": "✅ 발송", "pending": "⏳ 대기", "gave_up": "⛔ 포기"}
        for item in runner.plan(cfg, recipients, store, period):
            r = item.recipient
            label = labels[item.state] if not (item.state == "pending" and item.attempts) else f"⚠️ 실패 {item.attempts}회"
            tail = f" — {item.last_error[:70]}" if item.last_error else ""
            print(f"  {label:<10} {r.name:<10} {r.masked_phone} [{r.channel}]{tail}")
        print()
    finally:
        store.close()
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    cfg, _, store = _load(args)
    try:
        period = _period(args, cfg)
        phone = recipients_module.normalize_phone(args.phone) if args.phone else None
        if phone is None and not args.all:
            raise ConfigError("--phone 으로 한 명을 지정하거나, 그달 전체를 지우려면 --all 을 붙이세요")
        removed = store.reset(period, phone)
        print(f"{period} 기록 {removed}건 삭제 — 다음 `send` 에서 다시 발송됩니다.")
    finally:
        store.close()
    return 0


def cmd_telegram_link(args: argparse.Namespace) -> int:
    cfg, recipients, store = _load(args)
    token = (cfg.channels.get("telegram") or {}).get("bot_token", "")
    if not token:
        raise ConfigError("channels.telegram.bot_token 이 필요합니다")
    log.info("텔레그램 연결 대기 중 — 수신자가 봇에 /start 하면 번호 공유 버튼이 뜹니다. Ctrl+C 로 종료.")
    try:
        while True:
            try:
                telegram_link.poll_once(token, recipients, store)
            except ChannelError as exc:
                log.warning("%s — 10초 후 재시도", exc)
                time.sleep(10)
            if args.once:
                break
    except KeyboardInterrupt:
        log.info("사용자 중단")
    finally:
        store.close()
    return 0


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invite.py", description="매월 링크 자동 발송")
    parser.add_argument("-c", "--config", default="invite.json", help="설정 파일 (기본: invite.json)")
    parser.add_argument("--log-level", default="")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_period(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--period", default="", help="YYYY-MM (기본: 이번 달)")
        return p

    with_period(sub.add_parser("check", help="설정·명단 검증")).set_defaults(func=cmd_check)
    with_period(sub.add_parser("preview", help="나갈 메시지 미리보기")).set_defaults(func=cmd_preview)
    with_period(sub.add_parser("approve", help="그달 발송 승인")).set_defaults(func=cmd_approve)
    with_period(sub.add_parser("status", help="그달 발송 현황")).set_defaults(func=cmd_status)

    send = with_period(sub.add_parser("send", help="발송 (크론용)"))
    send.add_argument("--now", action="store_true", help="발송일 검사를 건너뛴다 (승인은 여전히 필요)")
    send.add_argument("--dry-run", action="store_true")
    send.set_defaults(func=cmd_send)

    reset = with_period(sub.add_parser("reset", help="발송 기록 삭제 → 재발송 가능"))
    reset.add_argument("--phone", default="")
    reset.add_argument("--all", action="store_true")
    reset.set_defaults(func=cmd_reset)

    link = sub.add_parser("telegram-link", help="텔레그램 수신자 번호 연결 대기")
    link.add_argument("--once", action="store_true", help="한 번만 확인하고 종료")
    link.set_defaults(func=cmd_telegram_link)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (ConfigError, RecipientError, ChannelError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
