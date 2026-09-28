#!/usr/bin/env python3
"""구독 만료 관리 사이트 + 만료 전 문자 자동 안내.

  python3 renew.py serve               # 관리 사이트 (기본 http://127.0.0.1:8090)
  python3 renew.py remind              # 크론용: 오늘 보낼 만료 안내 발송
  python3 renew.py remind --dry-run    # 누구에게 무엇이 나갈지만 출력
  python3 renew.py remind --force      # max_per_run 한도를 넘어도 발송 (확인 후에만)
  python3 renew.py list                # 고객 목록과 D-day

크론 예 (매일 10:05):
  5 10 * * *  cd /path/to/repo && python3 renew.py remind >> logs/renew.log 2>&1
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from familyinvite import channels as channels_module
from familyinvite.channels import ChannelError
from hotdeal.config import ConfigError
from renewal import config as config_module
from renewal import reminders, web
from renewal.dates import dday_label
from renewal.store import Store

log = logging.getLogger("renew")


def _setup_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%m-%d %H:%M:%S")


def _load(args: argparse.Namespace) -> config_module.RenewConfig:
    cfg = config_module.load(args.config)
    _setup_logging(args.log_level or cfg.log_level)
    return cfg


def _channel_factory(cfg: config_module.RenewConfig):
    def build() -> channels_module.Channel:
        return channels_module.build("sms", cfg.sms, lookup_chat_id=lambda _phone: None)

    build()  # 설정 오류는 시작 시점에 드러나게 한다
    return build


def cmd_serve(args: argparse.Namespace) -> int:
    cfg = _load(args)
    config_module.require_admin_password(cfg)
    app = web.App(cfg, _channel_factory(cfg))
    server = web.serve(app, args.host or cfg.admin_host, args.port or cfg.admin_port)
    if (args.host or cfg.admin_host) not in ("127.0.0.1", "localhost", "::1"):
        log.warning("외부에 열린 주소로 실행 중입니다. HTTPS 리버스 프록시 뒤에 두세요 (docs/renewal.md).")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("종료")
    finally:
        server.server_close()
    return 0


def cmd_remind(args: argparse.Namespace) -> int:
    cfg = _load(args)
    now = datetime.now(cfg.tz)
    if not cfg.auto_send and not args.dry_run and not args.force:
        log.info("auto_send=false — 자동 발송하지 않습니다. 사이트의 [문자 안내]에서 발송하세요.")
        return 0
    if not args.dry_run and not reminders.is_send_time(cfg, now):
        log.info("%d시 이전이라 보내지 않습니다 (send_hour).", cfg.send_hour)
        return 0
    with Store(cfg.db_path) as store:
        dues = reminders.due_reminders(cfg, store, now.date())
        if args.dry_run:
            for due in dues:
                print(f"── {due.customer.name} {due.customer.masked_phone} D-{due.days_before}\n{due.text}\n")
            print(f"[dry-run] 오늘 안내 대상 {len(dues)}명 (한도 {cfg.max_per_run}) — 보내지 않았습니다.")
            return 0
        if not dues:
            log.info("오늘 보낼 안내가 없습니다.")
            return 0
        report = reminders.send(dues, _channel_factory(cfg)(), store, max_per_run=cfg.max_per_run,
                                gap_seconds=cfg.send_gap_seconds, force=args.force)
    print(report.summary())
    return 1 if report.blocked_by_cap or ((report.failed or report.unknown) and not report.sent) else 0


def cmd_list(args: argparse.Namespace) -> int:
    cfg = _load(args)
    today = datetime.now(cfg.tz).date()
    with Store(cfg.db_path) as store:
        customers = store.customers()
    for c in customers:
        state = "" if c.active else " (중지)"
        print(f"{c.name:<10} {c.masked_phone}  {c.expires_on}  {dday_label(c.expires_on, today):<12}{state}")
    print(f"\n총 {len(customers)}명")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="renew.py", description="구독 만료 관리 + 문자 안내")
    parser.add_argument("-c", "--config", default="renew.json")
    parser.add_argument("--log-level", default="")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="관리 사이트 실행")
    serve.add_argument("--host", default="")
    serve.add_argument("--port", type=int, default=0)
    serve.set_defaults(func=cmd_serve)

    remind = sub.add_parser("remind", help="오늘 만료 안내 발송 (크론용)")
    remind.add_argument("--dry-run", action="store_true")
    remind.add_argument("--force", action="store_true", help="max_per_run 한도·auto_send=false 를 무시하고 발송")
    remind.set_defaults(func=cmd_remind)

    sub.add_parser("list", help="고객 목록").set_defaults(func=cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (ConfigError, ChannelError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
