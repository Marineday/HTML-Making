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

from . import allocator, capacity, cost, csvio, pool, proxy, proxychain, proxycheck, wireguard
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


def cmd_cost(args: argparse.Namespace) -> int:
    """데이터센터 고정 IP 와 레지덴셜 종량을 같은 워크로드에서 비교한다."""
    ip_count = pool.required_groups(args.users, args.per_ip)
    fleet = capacity.for_fleet(args.users, ip_count, _workload(args))
    transfer_gb = fleet.total_transfer_gb_month

    if args.dc_ip_monthly <= 0:
        print("오류: --dc-ip-monthly 에 실제 견적을 넣어야 비교할 수 있다.", file=sys.stderr)
        print("      예: --dc-ip-monthly 3.6 --dc-hosts 2 --dc-host-monthly 20 --res-per-gb 2.0", file=sys.stderr)
        return EXIT_FAIL

    dc = cost.estimate(
        "데이터센터",
        ip_count=ip_count,
        transfer_gb=transfer_gb,
        prices=cost.PriceBook(
            ip_monthly=args.dc_ip_monthly,
            host_monthly=args.dc_host_monthly,
            hosts=args.dc_hosts,
            egress_per_gb=args.dc_egress_per_gb,
        ),
    )
    # 레지덴셜을 써도 사용자가 인바운드로 붙을 호스트는 그대로 필요하다.
    res = cost.estimate(
        "레지덴셜",
        ip_count=ip_count,
        transfer_gb=transfer_gb,
        prices=cost.PriceBook(
            ip_monthly=args.res_ip_monthly,
            host_monthly=args.dc_host_monthly,
            hosts=args.dc_hosts,
            per_gb=args.res_per_gb,
        ),
    )

    print(f"\n워크로드: 사용자 {args.users:,}명 · 출구 IP {ip_count}개 · 월 전송량 {transfer_gb:,.0f} GB")
    print("\n[데이터센터 고정 IP]")
    print(_table(dc.table()))
    print("\n[레지덴셜]")
    print(_table(res.table()))
    print("  * 레지덴셜은 프록시 엔드포인트라 WireGuard 를 띄울 수 없다. 진입용")
    print("    데이터센터 호스트 비용이 양쪽에 똑같이 들어가 있다 — 대체가 아니라 추가다.")

    print("\n[판정]")
    for line in cost.compare(dc, res):
        print(f"  {line}")

    be = cost.breakeven_per_gb(dc.total, res.fixed, transfer_gb)
    if be is None:
        print(f"  레지덴셜은 고정비({res.fixed:,.2f})만으로 이미 데이터센터 총액({dc.total:,.2f})을 넘는다.")
        print("  GB 단가가 0이어도 이길 수 없는 구조다.")
    else:
        print(f"  손익분기 GB 단가: {be:.4f}")
        print(f"  이보다 싼 종량 단가를 받아야 레지덴셜이 유리하다. 현재 넣은 단가는 {args.res_per_gb:.4f} 다.")
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


def cmd_proxy_export(args: argparse.Namespace) -> int:
    """레지덴셜 프록시 경로용 서버·사용자 설정을 내보낸다."""
    assignments = csvio.read_assignments(args.assignments)
    tunnel = _tunnel(args)
    alloc = allocator.rebuild(assignments, group_size=args.per_ip, tunnel=tunnel)
    problems = allocator.verify(alloc)
    if problems:
        print("[검증 실패] 설정을 내보내지 않는다. 배정표를 먼저 고칠 것.")
        for p in problems:
            print(f"  ! {p}")
        return EXIT_FAIL

    proxies = proxy.dedupe_proxies(csvio.read_proxies(args.proxies))
    plaintext = [p.redacted() for p in proxies if p.needs_auth and p.password_env_var is None]
    if plaintext:
        print(f"  ! 평문 비밀번호가 {len(plaintext)}건 있다 ({plaintext[0]} …).")
        print("    CSV 의 password 컬럼에는 ${ENV_VAR} 형태로 적는 편이 안전하다.")

    opts = proxychain.ChainOptions(
        entry_host=args.entry_host,
        entry_port=args.entry_port,
        wg_interface=args.wg_interface,
        redsocks_base_port=args.redsocks_base_port,
        mtu=args.mtu,
        salt=args.salt,
    )
    counts = proxychain.export(
        alloc,
        proxies,
        args.out,
        tunnel=tunnel,
        opts=opts,
        client_opts=wireguard.ExportOptions(allowed_ips=args.allowed_ips, dns=args.dns, mtu=args.mtu),
    )

    out = Path(args.out)
    print(f"묶음 {counts['groups']}개, 사용자 설정 {counts['clients']}개를 {out} 에 썼다.")
    print(f"  진입 호스트: {args.entry_host}:{args.entry_port} (공인 IP 1개면 된다)")
    print("  배정표의 egress_ip 는 이 경로에서 쓰이지 않는다 — 출구는 프록시가 정한다.")
    print("\n  서버에서 순서대로:")
    print(f"    sh {out / 'genkeys.sh'}       # WireGuard 키")
    print(f"    sh {out / 'fill-secrets.sh'}  # 프록시 비밀번호 (환경변수 필요)")
    print(f"    sh {out / 'iptables.sh'} up   # 묶음별 경로 + UDP 정책")
    print("\n  확인 안 하고 운영에 넣지 말 것:")
    print(f"    python3 -m ipalloc proxy-check --assignments {args.assignments} --proxies {args.proxies}")
    return EXIT_OK


def cmd_proxy_check(args: argparse.Namespace) -> int:
    """묶음별로 프록시를 통해 실제 출구 IP 를 확인한다."""
    assignments = csvio.read_assignments(args.assignments)
    alloc = allocator.rebuild(assignments, group_size=args.per_ip, tunnel=_tunnel(args))
    groups = alloc.used_groups
    proxies = proxy.dedupe_proxies(csvio.read_proxies(args.proxies))
    if len(proxies) < len(groups):
        print(f"오류: 묶음 {len(groups)}개에 프록시는 {len(proxies)}개뿐이다.", file=sys.stderr)
        return EXIT_FAIL

    limit = args.limit if args.limit > 0 else len(groups)
    results: list[proxycheck.CheckResult] = []
    print(f"에코 URL: {args.echo_url}")
    print(f"확인 대상: 묶음 {min(limit, len(groups))}개\n")
    for group, upstream in list(zip(groups, proxies))[:limit]:
        result = proxycheck.check_group(
            group.id,
            upstream.host,
            upstream.port,
            username=proxy.sticky_username(upstream, group.id, salt=args.salt),
            password=upstream.resolve_password() if upstream.needs_auth else "",
            echo_url=args.echo_url,
            timeout=args.timeout,
        )
        results.append(result)
        print(f"  {result.line()}")

    print()
    for line in proxycheck.summarize(results):
        print(f"  {line}")
    if args.out:
        _write_check_results(args.out, results)
        print(f"\n  {args.out} 에 기록했다. 시간을 두고 다시 돌린 뒤 proxy-sticky 로 비교할 것.")
    return EXIT_OK if all(r.ok for r in results) else EXIT_FAIL


def cmd_proxy_sticky(args: argparse.Namespace) -> int:
    """두 번의 proxy-check 결과를 비교해 sticky 유지 여부를 본다."""
    before = _read_check_results(args.before)
    after = _read_check_results(args.after)
    for line in proxycheck.compare_runs(before, after):
        print(f"  {line}")
    return EXIT_OK


def _write_check_results(path: str, results: list[proxycheck.CheckResult]) -> None:
    import csv

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["group_id", "ok", "egress_ip", "elapsed_ms", "error"])
        writer.writeheader()
        for r in results:
            writer.writerow(
                {"group_id": r.group_id, "ok": "1" if r.ok else "0", "egress_ip": r.egress_ip, "elapsed_ms": str(r.elapsed_ms), "error": r.error}
            )


def _read_check_results(path: str) -> list[proxycheck.CheckResult]:
    import csv

    p = Path(path)
    if not p.exists():
        raise csvio.CsvError(f"파일이 없다: {p}")
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        return [
            proxycheck.CheckResult(
                group_id=row.get("group_id", ""),
                ok=row.get("ok") == "1",
                egress_ip=row.get("egress_ip", ""),
                error=row.get("error", ""),
            )
            for row in csv.DictReader(fh)
            if row.get("group_id")
        ]


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

    p = sub.add_parser("cost", help="데이터센터 고정 IP 와 레지덴셜 종량의 월 비용을 비교한다")
    p.add_argument("--users", type=int, required=True)
    p.add_argument("--dc-ip-monthly", type=float, default=0.0, help="데이터센터 IP 1개의 월정액 (필수)")
    p.add_argument("--dc-hosts", type=int, default=1, help="진입용 호스트 대수. 기본 1")
    p.add_argument("--dc-host-monthly", type=float, default=0.0, help="호스트 1대의 월정액")
    p.add_argument("--dc-egress-per-gb", type=float, default=0.0, help="전송량 GB 당 단가. 전송량 포함 VPS 면 0")
    p.add_argument("--res-per-gb", type=float, default=0.0, help="레지덴셜 종량 GB 당 단가")
    p.add_argument("--res-ip-monthly", type=float, default=0.0, help="static ISP 프록시처럼 IP 월정액이 있는 경우")
    _add_tunnel_args(p)
    _add_capacity_args(p)
    p.set_defaults(func=cmd_cost)

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

    p = sub.add_parser("proxy-export", help="레지덴셜 프록시 경로용 서버·사용자 설정을 내보낸다")
    p.add_argument("--assignments", required=True)
    p.add_argument("--proxies", required=True, help="업스트림 프록시 CSV (host, port 컬럼 필요)")
    p.add_argument("--entry-host", required=True, help="사용자가 접속할 진입 호스트. 공인 IP 1개면 된다")
    p.add_argument("--entry-port", type=int, default=51820)
    p.add_argument("--out", default="out/proxy")
    p.add_argument("--wg-interface", default="wg0")
    p.add_argument("--redsocks-base-port", type=int, default=proxychain.DEFAULT_REDSOCKS_BASE_PORT)
    p.add_argument("--allowed-ips", default=wireguard.DEFAULT_ALLOWED_IPS)
    p.add_argument("--dns", default=wireguard.DEFAULT_DNS, help="사용자 설정의 DNS. 프록시를 타지 않고 진입 호스트 경로로 나간다")
    p.add_argument("--mtu", type=int, default=wireguard.DEFAULT_MTU)
    p.add_argument("--salt", default="", help="바꾸면 전 묶음의 sticky 세션이 한꺼번에 갈린다")
    _add_tunnel_args(p)
    p.set_defaults(func=cmd_proxy_export)

    p = sub.add_parser("proxy-check", help="묶음별 실제 출구 IP 를 확인한다 (네트워크 필요)")
    p.add_argument("--assignments", required=True)
    p.add_argument("--proxies", required=True)
    p.add_argument("--echo-url", default=proxycheck.DEFAULT_ECHO_URL, help="출구 IP 를 돌려주는 http:// URL. 자체 엔드포인트를 쓰는 편이 정확하다")
    p.add_argument("--timeout", type=float, default=proxycheck.DEFAULT_TIMEOUT)
    p.add_argument("--limit", type=int, default=0, help="앞에서 N개 묶음만 확인한다. 0 이면 전부")
    p.add_argument("--salt", default="")
    p.add_argument("--out", help="결과를 CSV 로 기록한다. proxy-sticky 비교에 쓴다")
    _add_tunnel_args(p)
    p.set_defaults(func=cmd_proxy_check)

    p = sub.add_parser("proxy-sticky", help="두 번의 proxy-check 결과를 비교해 sticky 유지 여부를 본다")
    p.add_argument("--before", required=True)
    p.add_argument("--after", required=True)
    p.set_defaults(func=cmd_proxy_sticky)

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
        cost.CostError,
        proxy.ProxyError,
        proxychain.ProxyChainError,
        proxycheck.ProxyCheckError,
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
