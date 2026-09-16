"""ipalloc CLI.

  python3 -m ipalloc plan --users 1000 --per-ip 20
  python3 -m ipalloc sample-users --count 1000 --out users.csv
  python3 -m ipalloc assign --users users.csv --endpoints endpoints.csv --out out/
  python3 -m ipalloc verify --assignments out/assignments.csv --per-ip 20
  python3 -m ipalloc wg --assignments out/assignments.csv --out out/wg/
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import allocator, capacity, csvio, pool, wireguard
from .models import ModelError, User

EXIT_OK = 0
EXIT_FAIL = 1


def _table(rows: list[tuple[str, str]], indent: str = "  ") -> str:
    if not rows:
        return ""
    width = max(len(k) for k, _ in rows)
    return "\n".join(f"{indent}{k.ljust(width)}  {v}" for k, v in rows)


def _workload(args: argparse.Namespace) -> capacity.Workload:
    return capacity.Workload(
        users_per_ip=args.per_ip,
        minutes_per_user_per_month=args.minutes,
        stream_mbps=args.mbps,
        peak_concentration=args.peak,
    )


def _tunnel(args: argparse.Namespace) -> pool.TunnelPlan:
    return pool.TunnelPlan(base_cidr=args.tunnel_cidr, group_prefix=args.group_prefix)


def _print_capacity(user_count: int, ip_count: int, load: capacity.Workload, link_mbps: float, quota_gb: float | None) -> None:
    fleet = capacity.for_fleet(user_count, ip_count, load)
    print("\n[규모]")
    print(_table(fleet.table()))
    print("\n[출구 IP 1개 기준]")
    print(_table(fleet.per_ip.table()))
    problems = capacity.verdict(fleet.per_ip, link_mbps=link_mbps, transfer_quota_gb=quota_gb)
    print(f"\n[판정] 회선 {link_mbps:.0f} Mbps 기준")
    if problems:
        for p in problems:
            print(f"  ! {p}")
    else:
        print(f"  여유 있다. 최악(전원 동시 {fleet.per_ip.concurrency_worst}명)에도 " f"{fleet.per_ip.mbps_worst:.0f} Mbps 면 된다.")
    print(
        "\n  주의: 위 전송량은 사용자가 '영상만' 볼 때의 값이다. 전체 터널"
        "(AllowedIPs 0.0.0.0/0)을\n  쓰면 VPN 을 켜둔 동안의 모든 트래픽이 같이 지나가므로 이 추정은 의미가 없어진다.\n"
        "  서비스 대역이 고정이면 `wg --allowed-ips <대역>` 으로 분할 터널을 쓸 것."
    )


def cmd_plan(args: argparse.Namespace) -> int:
    need = pool.required_groups(args.users, args.per_ip)
    tunnel = _tunnel(args)
    print(f"사용자 {args.users:,}명을 {args.per_ip}명씩 묶으면 출구 IP {need}개가 필요하다.")
    print(f"터널 대역 {tunnel.base_cidr} 는 묶음 {tunnel.capacity}개 / 묶음당 {tunnel.hosts_per_group}명까지 수용한다.")
    if need > tunnel.capacity:
        print("  ! 터널 대역이 부족하다. 더 큰 사설 대역이나 더 작은 프리픽스를 쓸 것.")
    _print_capacity(args.users, need, _workload(args), args.link_mbps, args.quota_gb)
    return EXIT_OK


def cmd_capacity(args: argparse.Namespace) -> int:
    need = pool.required_groups(args.users, args.per_ip)
    _print_capacity(args.users, need, _workload(args), args.link_mbps, args.quota_gb)
    return EXIT_OK


def cmd_sample_users(args: argparse.Namespace) -> int:
    users = [User(id=f"{args.prefix}{i:04d}") for i in range(1, args.count + 1)]
    n = csvio.write_users(args.out, users)
    print(f"{args.out} 에 샘플 사용자 {n}명을 썼다. label 컬럼은 비어 있다 — 실제 명부로 바꿔 쓸 것.")
    return EXIT_OK


def cmd_assign(args: argparse.Namespace) -> int:
    users = csvio.read_users(args.users)
    if args.endpoints:
        endpoints = csvio.read_endpoints(args.endpoints)
    else:
        endpoints = pool.endpoints_from_cidr(args.cidr, region=args.region, listen_port=args.port)

    doc_ips = [ep.ip for ep in endpoints if ep.is_documentation]
    if doc_ips:
        print(f"  ! 예제용(RFC5737) 주소 {len(doc_ips)}개가 섞여 있다 ({doc_ips[0]} …). " "실습용이면 그대로 두고, 운영이면 실제 할당받은 IP 로 바꿀 것.")

    existing = csvio.read_assignments(args.existing) if args.existing else []
    tunnel = _tunnel(args)

    if not args.allow_partial:
        pool.check_pool(len(users), args.per_ip, len(endpoints))

    alloc = allocator.allocate(users, endpoints, group_size=args.per_ip, tunnel=tunnel, existing=existing)

    out = Path(args.out)
    a_path = out / "assignments.csv"
    g_path = out / "groups.csv"
    csvio.write_assignments(a_path, alloc.assignments)
    csvio.write_groups(g_path, alloc.used_groups, args.per_ip, tunnel)

    print(alloc.summary())
    print(f"  {a_path}")
    print(f"  {g_path}")
    if alloc.dropped:
        shown = ", ".join(alloc.dropped[:10]) + (" …" if len(alloc.dropped) > 10 else "")
        print(f"  명부에서 빠진 사용자 {len(alloc.dropped)}명의 자리를 비웠다: {shown}")
        print("  이 사람들의 설정은 서버에서 [Peer] 블록을 지워야 실제로 차단된다.")
    if alloc.moved:
        shown = ", ".join(alloc.moved[:10]) + (" …" if len(alloc.moved) > 10 else "")
        print(f"  쓰던 출구 IP 가 사라져 {len(alloc.moved)}명을 재배정했다: {shown}")
        print("  이 사람들에게는 설정 파일을 다시 배포해야 한다.")

    problems = allocator.verify(alloc)
    if problems:
        print("\n[검증 실패]")
        for p in problems:
            print(f"  ! {p}")
        return EXIT_FAIL

    _print_capacity(len(alloc.assignments), len(alloc.used_groups), _workload(args), args.link_mbps, args.quota_gb)
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    assignments = csvio.read_assignments(args.assignments)
    alloc = allocator.rebuild(assignments, group_size=args.per_ip, tunnel=_tunnel(args))
    problems = allocator.verify(alloc)
    if problems:
        print(f"[검증 실패] 문제 {len(problems)}건")
        for p in problems:
            print(f"  ! {p}")
        return EXIT_FAIL
    print(f"[정상] 사용자 {len(alloc.assignments)}명 · 묶음 {len(alloc.used_groups)}개 · " f"묶음당 최대 {args.per_ip}명 — 중복·정원초과·서브넷 이탈 없음")
    return EXIT_OK


def cmd_wg(args: argparse.Namespace) -> int:
    assignments = csvio.read_assignments(args.assignments)
    tunnel = _tunnel(args)
    alloc = allocator.rebuild(assignments, group_size=args.per_ip, tunnel=tunnel)
    problems = allocator.verify(alloc)
    if problems:
        print("[검증 실패] 설정을 내보내지 않는다. 배정표를 먼저 고칠 것.")
        for p in problems:
            print(f"  ! {p}")
        return EXIT_FAIL

    opts = wireguard.ExportOptions(
        dns=args.dns,
        allowed_ips=args.allowed_ips,
        wan_interface=args.wan,
        mtu=args.mtu,
    )
    counts = wireguard.export(alloc, args.out, tunnel=tunnel, opts=opts)
    print(f"서버 설정 {counts['servers']}개, 사용자 설정 {counts['clients']}개를 {args.out} 에 썼다.")
    print(f"  키가 아직 없다. 다음을 돌려야 동작한다:  sh {Path(args.out) / 'genkeys.sh'}")
    if opts.allowed_ips.strip() in ("0.0.0.0/0", "::/0"):
        print("  AllowedIPs 가 전체 터널이다. 사용자의 모든 트래픽이 출구 IP 로 나간다 — " "전송량 추정이 크게 빗나갈 수 있다.")
    return EXIT_OK


def _add_capacity_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--minutes", type=float, default=20.0, help="1인당 월 접속 시간(분). 기본 20")
    p.add_argument("--mbps", type=float, default=capacity.BITRATE_MBPS["1080p"], help="스트림 비트레이트(Mbps). 기본 5.0 (1080p)")
    p.add_argument("--peak", type=float, default=0.10, help="접속이 몰리는 시간대 비율. 기본 0.10")
    p.add_argument("--link-mbps", type=float, default=1000.0, help="출구 IP 1개에 붙일 회선 속도(Mbps). 기본 1000")
    p.add_argument("--quota-gb", type=float, default=None, help="출구 IP 1개의 월 전송량 한도(GB). 지정하면 초과 여부를 본다")


def _add_tunnel_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--per-ip", type=int, default=allocator.DEFAULT_GROUP_SIZE, help="출구 IP 하나를 공유하는 인원. 기본 20")
    p.add_argument("--tunnel-cidr", default=pool.DEFAULT_TUNNEL_CIDR, help=f"터널 사설 대역. 기본 {pool.DEFAULT_TUNNEL_CIDR}")
    p.add_argument("--group-prefix", type=int, default=pool.DEFAULT_GROUP_PREFIX, help="묶음당 서브넷 프리픽스. 기본 24")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ipalloc", description="VPN 출구 IP 배정표 생성기")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="필요한 출구 IP 개수와 용량을 산정한다 (파일 불필요)")
    p.add_argument("--users", type=int, required=True, help="총 사용자 수")
    _add_tunnel_args(p)
    _add_capacity_args(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("capacity", help="용량만 계산한다")
    p.add_argument("--users", type=int, required=True)
    _add_tunnel_args(p)
    _add_capacity_args(p)
    p.set_defaults(func=cmd_capacity)

    p = sub.add_parser("sample-users", help="테스트용 명부 CSV 를 만든다")
    p.add_argument("--count", type=int, default=1000)
    p.add_argument("--prefix", default="u")
    p.add_argument("--out", default="users.csv")
    p.set_defaults(func=cmd_sample_users)

    p = sub.add_parser("assign", help="배정표를 만든다")
    p.add_argument("--users", required=True, help="명부 CSV (user_id 컬럼 필요)")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--endpoints", help="출구 IP 목록 CSV (ip 컬럼 필요)")
    src.add_argument("--cidr", help="출구 IP 대역. 예: 203.0.113.0/26")
    p.add_argument("--region", default="", help="--cidr 로 줄 때 붙일 리전 표기")
    p.add_argument("--port", type=int, default=51820, help="--cidr 로 줄 때의 WireGuard 포트")
    p.add_argument("--existing", help="기존 배정표 CSV. 주면 증분 배정한다 (기존 사용자는 움직이지 않는다)")
    p.add_argument("--out", default="out", help="출력 디렉터리. 기본 out/")
    p.add_argument("--allow-partial", action="store_true", help="IP 가 모자라도 배정하고 미배정자를 보고한다")
    _add_tunnel_args(p)
    _add_capacity_args(p)
    p.set_defaults(func=cmd_assign)

    p = sub.add_parser("verify", help="배정표의 불변식을 검사한다")
    p.add_argument("--assignments", required=True)
    _add_tunnel_args(p)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("wg", help="배정표를 WireGuard 설정으로 내보낸다")
    p.add_argument("--assignments", required=True)
    p.add_argument("--out", default="out/wg")
    p.add_argument("--dns", default=wireguard.DEFAULT_DNS)
    p.add_argument("--allowed-ips", default=wireguard.DEFAULT_ALLOWED_IPS, help="사용자 설정의 AllowedIPs. 서비스 대역만 주면 분할 터널이 된다")
    p.add_argument("--wan", default=wireguard.DEFAULT_WAN_INTERFACE, help="서버의 공인 인터페이스 이름")
    p.add_argument("--mtu", type=int, default=wireguard.DEFAULT_MTU)
    _add_tunnel_args(p)
    p.set_defaults(func=cmd_wg)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (
        ModelError,
        csvio.CsvError,
        pool.PoolError,
        allocator.AllocationError,
        capacity.CapacityError,
    ) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except OSError as exc:
        # 쓸 수 없는 --out 경로, 권한 없음, 디스크 가득 참 등. 역추적을 그대로
        # 뱉는 것보다 무엇이 잘못됐는지 한 줄로 말하는 편이 낫다.
        print(f"오류: 파일을 쓰거나 읽을 수 없다 — {exc}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
