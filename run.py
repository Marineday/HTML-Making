#!/usr/bin/env python3
"""핫딜 봇 CLI.

  python3 run.py once            # 1회 수집·발송
  python3 run.py once --dry-run  # 발송 없이 무엇이 나갈지만 확인
  python3 run.py loop            # 주기 폴링 (상시 운영)
  python3 run.py seed            # 발송 없이 현재 피드를 기준선으로 기록
  python3 run.py verify          # 각 소스가 실제로 살아있는지 점검
  python3 run.py bridge          # 카카오 브리지 큐 서버 실행
"""

from __future__ import annotations

import argparse
import logging
import sys

from hotdeal import config as config_module
from hotdeal.formatter import format_deal
from hotdeal.http import DEFAULT_USER_AGENT, HttpClient
from hotdeal.pipeline import Pipeline
from hotdeal.senders.base import SendError
from hotdeal.sources import build as build_source
from hotdeal.sources.base import SourceError

log = logging.getLogger("run")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_once(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    _setup_logging(args.log_level or cfg.log_level)
    pipeline = Pipeline(cfg)
    try:
        report = pipeline.run_once(dry_run=args.dry_run)
    finally:
        pipeline.close()
    log.info("결과: %s", report.summary())
    return 1 if (report.source_errors and not report.collected) else 0


def cmd_loop(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    _setup_logging(args.log_level or cfg.log_level)
    pipeline = Pipeline(cfg)
    try:
        pipeline.run_forever(dry_run=args.dry_run)
    except KeyboardInterrupt:
        log.info("사용자 중단")
    finally:
        pipeline.close()
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    cfg = config_module.load(args.config)
    _setup_logging(args.log_level or cfg.log_level)
    pipeline = Pipeline(cfg)
    try:
        report = pipeline.run_once(force_seed=True)
    finally:
        pipeline.close()
    log.info("기준선 기록 완료: %s", report.summary())
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """각 소스를 실제로 한 번씩 때려보고 살아있는지 표로 보고한다.

    핫딜 커뮤니티의 RSS 주소는 예고 없이 바뀐다. 봇을 붙이기 전에 이걸로
    먼저 확인하는 것이 정상 절차다.
    """
    cfg = config_module.load(args.config)
    _setup_logging(args.log_level or cfg.log_level)
    client = HttpClient(
        user_agent=cfg.user_agent or DEFAULT_USER_AGENT,
        respect_robots=cfg.respect_robots,
        min_interval_per_host=cfg.min_interval_per_host,
    )

    rows: list[tuple[str, str, str]] = []
    failures = 0
    for source_config in cfg.sources:
        if not source_config.enabled:
            rows.append((source_config.name, "SKIP", "enabled=false"))
            continue
        source = build_source(source_config, client)
        try:
            deals = source.fetch()
        except SourceError as exc:
            rows.append((source_config.name, "FAIL", str(exc)[:110]))
            failures += 1
            continue
        except Exception as exc:  # noqa: BLE001
            rows.append((source_config.name, "FAIL", repr(exc)[:110]))
            failures += 1
            continue

        if not deals:
            rows.append((source_config.name, "EMPTY", "응답은 왔지만 항목 0건 — 주소/패턴 확인 필요"))
            failures += 1
        else:
            rows.append((source_config.name, "OK", f"{len(deals)}건, 예: {deals[0].title[:60]}"))
            if args.show:
                for deal in deals[: args.show]:
                    print(format_deal(deal, "plain"))
                    print()

    width = max((len(name) for name, _, _ in rows), default=10)
    print()
    print(f"{'소스'.ljust(width)}  상태     비고")
    print("-" * (width + 60))
    for name, status, note in rows:
        print(f"{name.ljust(width)}  {status.ljust(6)}  {note}")
    print()
    if failures:
        print(f"⚠️  {failures}개 소스에 문제가 있습니다. config.json 의 url / link_pattern 을 고치세요.")
    return 1 if failures else 0


def cmd_bridge(args: argparse.Namespace) -> int:
    from bridge.server import main as bridge_main

    forwarded = ["--host", args.host, "--port", str(args.port), "--db", args.db]
    return bridge_main(forwarded)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run.py", description="핫딜 봇")
    parser.add_argument("-c", "--config", default="config.json", help="설정 파일 경로 (기본: config.json)")
    parser.add_argument("--log-level", default="", help="DEBUG/INFO/WARNING/ERROR")
    sub = parser.add_subparsers(dest="command", required=True)

    once = sub.add_parser("once", help="1회 수집·발송")
    once.add_argument("--dry-run", action="store_true", help="실제 발송 없이 대상만 출력")
    once.set_defaults(func=cmd_once)

    loop = sub.add_parser("loop", help="주기 폴링 (상시 운영)")
    loop.add_argument("--dry-run", action="store_true")
    loop.set_defaults(func=cmd_loop)

    seed = sub.add_parser("seed", help="발송 없이 현재 피드를 기준선으로 기록")
    seed.set_defaults(func=cmd_seed)

    verify = sub.add_parser("verify", help="소스가 실제로 살아있는지 점검")
    verify.add_argument("--show", type=int, default=0, help="소스별로 N건 미리보기 출력")
    verify.set_defaults(func=cmd_verify)

    bridge = sub.add_parser("bridge", help="카카오 브리지 큐 서버 실행 (BRIDGE_TOKEN 환경변수 필요)")
    bridge.add_argument("--host", default="0.0.0.0")
    bridge.add_argument("--port", type=int, default=8080)
    bridge.add_argument("--db", default="bridge.db")
    bridge.set_defaults(func=cmd_bridge)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except config_module.ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2
    except SendError as exc:
        print(f"전송 어댑터 설정 오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
