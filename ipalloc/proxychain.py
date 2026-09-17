"""레지덴셜 프록시 경로용 서버 구성 생성.

    사용자 → WireGuard 진입 호스트(공인 IP 1개) → 묶음별 redsocks → 묶음별
    sticky 레지덴셜 세션 → 서비스

데이터센터 방식과 달라지는 점
    출구를 구분하는 것이 진입 IP 가 아니라 프록시라서, **진입 IP 는 1개면
    된다.** WireGuard 인터페이스도 하나만 띄우고, 묶음 구분은 터널 서브넷으로
    한다. iptables 가 출발지 서브넷을 보고 그 묶음의 redsocks 포트로 보낸다.

UDP 가 죽는다 — 이게 이 경로의 가장 큰 함정
    SOCKS5 의 UDP ASSOCIATE 를 지원하는 레지덴셜 공급사는 드물다. 그런데
    요즘 영상 스트리밍은 QUIC(UDP 443)을 먼저 쓴다. 그대로 두면 재생이
    안 되거나 한참 기다렸다 붙는다.

    그래서 생성되는 iptables 규칙은 터널에서 나가는 UDP 443 을 **거부**한다.
    거부(reject)여야 클라이언트가 즉시 TCP 로 되돌아간다 — drop 하면 타임아웃을
    기다리느라 체감이 더 나빠진다.

    DNS 는 프록시를 타지 않고 진입 호스트의 일반 경로로 나간다. 조회 출처가
    진입 호스트 IP 가 되지만, 자체 서비스용이므로 숨길 이유가 없고 UDP 를
    SOCKS5 에 태우는 것보다 훨씬 안정적이다.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, replace
from pathlib import Path

from .models import Allocation, Group
from .pool import TunnelPlan
from .proxy import ProxyEndpoint, sticky_username
from .wireguard import ExportOptions, render_client_conf

DEFAULT_REDSOCKS_BASE_PORT = 12000
# 프록시로 보내면 안 되는 목적지. 사설 대역과 루프백은 그대로 내보낸다.
LOCAL_DESTINATIONS = ("0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4")


class ProxyChainError(ValueError):
    """체인 구성 오류."""


@dataclass(frozen=True)
class ChainOptions:
    """진입 호스트와 체인 구성."""

    entry_host: str
    entry_port: int = 51820
    wg_interface: str = "wg0"
    redsocks_base_port: int = DEFAULT_REDSOCKS_BASE_PORT
    mtu: int = 1420
    salt: str = ""

    def __post_init__(self) -> None:
        if not self.entry_host.strip():
            raise ProxyChainError("진입 호스트를 지정해야 한다. 사용자가 붙을 주소다.")
        if not 1 <= self.entry_port <= 65535:
            raise ProxyChainError(f"진입 포트 {self.entry_port} 는 범위를 벗어났다.")
        if not 1024 <= self.redsocks_base_port <= 60000:
            raise ProxyChainError("redsocks 시작 포트는 1024~60000 사이여야 한다.")

    def redsocks_port(self, index: int) -> int:
        port = self.redsocks_base_port + index
        if port > 65535:
            raise ProxyChainError(f"redsocks 포트가 65535 를 넘었다({port}). 시작 포트를 낮출 것.")
        return port


def render_wg_server(allocation: Allocation, tunnel: TunnelPlan, opts: ChainOptions) -> str:
    """진입 호스트의 WireGuard 설정. 인터페이스 하나에 전 사용자를 담는다.

    SNAT 규칙이 없다 — 출구는 iptables 가 redsocks 로 보낸 뒤 프록시가 정한다.
    """
    base = ipaddress.ip_network(tunnel.base_cidr)
    lines = [
        f"# 진입 호스트 {opts.entry_host}:{opts.entry_port}",
        f"# 묶음 {len(allocation.used_groups)}개 · 사용자 {len(allocation.assignments)}명",
        "# genkeys.sh 로 키를 채우고, iptables.sh 로 묶음별 경로를 걸어야 동작한다.",
        "",
        "[Interface]",
        f"Address = {tunnel.server_ip(base)}/{base.prefixlen}",
        f"ListenPort = {opts.entry_port}",
        "PrivateKey = __SERVER_PRIVKEY__",
        f"MTU = {opts.mtu}",
    ]
    for group in allocation.used_groups:
        for m in sorted(group.members, key=lambda a: a.user_id):
            lines += [
                "",
                f"[Peer]  # {m.user_id}" + (f" ({m.label})" if m.label else "") + f" · {group.id}",
                f"PublicKey = __CLIENT_PUBKEY_{m.user_id}__",
                f"PresharedKey = __CLIENT_PSK_{m.user_id}__",
                f"AllowedIPs = {m.tunnel_ip}/32",
            ]
    return "\n".join(lines) + "\n"


def render_redsocks_conf(group: Group, port: int, proxy: ProxyEndpoint, *, salt: str = "") -> str:
    """묶음 하나의 redsocks 설정.

    비밀번호는 자리표시자로 둔다. 서버에서 fill-secrets.sh 가 채운다.
    """
    login = sticky_username(proxy, group.id, salt=salt)
    lines = [
        f"// 묶음 {group.id} · {group.subnet} → {proxy.redacted()}",
        f"// sticky 세션: {login or '(인증 없음)'}",
        "base {",
        "    log_debug = off;",
        "    log_info = on;",
        "    daemon = on;",
        "    redirector = iptables;",
        "}",
        "",
        "redsocks {",
        "    local_ip = 127.0.0.1;",
        f"    local_port = {port};",
        f"    ip = {proxy.host};",
        f"    port = {proxy.port};",
        f"    type = {proxy.protocol};",
    ]
    if proxy.needs_auth:
        lines += [
            f'    login = "{login}";',
            f'    password = "__PROXY_PASSWORD_{group.id}__";',
        ]
    lines.append("}")
    return "\n".join(lines) + "\n"


def render_iptables_sh(allocation: Allocation, opts: ChainOptions) -> str:
    """묶음별 서브넷을 자기 redsocks 포트로 보내는 규칙."""
    head = [
        "#!/bin/sh",
        "# 묶음별 TCP 를 그 묶음의 redsocks 로 넘긴다.",
        "#",
        "#   sh iptables.sh up     규칙 적용",
        "#   sh iptables.sh down   규칙 제거",
        "#",
        "# 먼저 켜둘 것: sysctl -w net.ipv4.ip_forward=1",
        "set -eu",
        "",
        f'WG="{opts.wg_interface}"',
        'ACTION="${1:-up}"',
        'if [ "$ACTION" = "up" ]; then OP="-A"; NOP="-I"; elif [ "$ACTION" = "down" ]; then OP="-D"; NOP="-D"; else',
        '  echo "사용법: sh iptables.sh [up|down]" >&2; exit 1',
        "fi",
        "",
        "# 철거할 때는 규칙 하나가 없어도 나머지를 마저 지워야 한다. set -e 아래에서",
        "# 그냥 두면 첫 실패에 멈춰 절반만 지워진 상태가 남는다.",
        "run() {",
        '  if [ "$ACTION" = "down" ]; then',
        '    iptables "$@" 2>/dev/null || true',
        "  else",
        '    iptables "$@"',
        "  fi",
        "}",
        "",
        "# 중복 적용을 막기 위해 up 전에 기존 규칙을 먼저 걷어낸다.",
        'if [ "$ACTION" = "up" ]; then',
        '  sh "$0" down >/dev/null 2>&1 || true',
        "fi",
        "",
        "# 사설/루프백 목적지는 프록시를 태우지 않는다.",
    ]

    body: list[str] = []
    for net in LOCAL_DESTINATIONS:
        body.append(f'run -t nat $NOP PREROUTING -i "$WG" -d {net} -j RETURN')

    body += [
        "",
        "# DNS 는 프록시를 타지 않고 진입 호스트의 일반 경로로 나간다.",
        "# 조회 출처가 진입 호스트 IP 가 된다 — 자체 서비스이므로 숨길 이유가 없고,",
        "# UDP 를 SOCKS5 에 태우는 것보다 훨씬 안정적이다.",
        'run -t nat $OP PREROUTING -i "$WG" -p udp --dport 53 -j RETURN',
        "",
        "# QUIC(UDP 443)을 거부해 클라이언트를 TCP 로 되돌린다.",
        "# drop 이 아니라 reject 여야 즉시 폴백한다 — drop 이면 타임아웃을 기다린다.",
        'run $OP FORWARD -i "$WG" -p udp --dport 443 -j REJECT --reject-with icmp-port-unreachable',
        "",
        "# 묶음별 경로",
    ]
    for index, group in enumerate(allocation.used_groups):
        port = opts.redsocks_port(index)
        body.append(f'run -t nat $OP PREROUTING -i "$WG" -s {group.subnet} -p tcp -j REDIRECT --to-ports {port}  # {group.id}')

    tail = [
        "",
        'echo "묶음 ' + str(len(allocation.used_groups)) + ' 개 규칙을 $ACTION 했다."',
    ]
    return "\n".join(head + body + tail) + "\n"


def render_genkeys_sh() -> str:
    """진입 서버가 하나인 구성용 키 생성 스크립트.

    wireguard.render_genkeys_sh 는 묶음마다 서버 설정이 따로 있는 구성을
    전제한다. 여기서는 wg0.conf 하나에 전 사용자 peer 가 들어가므로 별도다.
    """
    return r"""#!/bin/sh
# WireGuard 키 생성 + wg0.conf / clients 의 자리표시자 치환.
#
#   sh genkeys.sh
#
# - wireguard-tools (wg 명령) 가 필요하다.
# - 이미 keys/ 에 있는 키는 재사용한다. 두 번 돌려도 안전하다.
# - keys/ 에는 개인키가 들어간다. 절대 저장소에 올리지 말 것.
set -eu
cd "$(dirname "$0")"
umask 077

command -v wg >/dev/null 2>&1 || { echo "wg 명령이 없다. wireguard-tools 를 설치할 것." >&2; exit 1; }
[ -f peers.csv ] || { echo "peers.csv 가 없다. 내보내기를 다시 할 것." >&2; exit 1; }

mkdir -p keys

# 1) 진입 서버 키 (하나뿐이다)
[ -s keys/server.key ] || wg genkey > keys/server.key
wg pubkey < keys/server.key > keys/server.pub

# 2) 사용자 키와 사전공유키
tail -n +2 peers.csv | while IFS=, read -r uid grp; do
  [ -n "$uid" ] || continue
  [ -s "keys/$uid.key" ] || wg genkey > "keys/$uid.key"
  wg pubkey < "keys/$uid.key" > "keys/$uid.pub"
  [ -s "keys/$uid.psk" ] || wg genpsk > "keys/$uid.psk"
done

# 3) 사용자 설정 치환
tail -n +2 peers.csv | while IFS=, read -r uid grp; do
  [ -n "$uid" ] || continue
  conf="clients/$grp/$uid.conf"
  [ -f "$conf" ] || { echo "설정 파일이 없다: $conf" >&2; exit 1; }
  grep -q '__CLIENT_PRIVKEY__' "$conf" || continue
  sed -e "s|__CLIENT_PRIVKEY__|$(cat "keys/$uid.key")|" \
      -e "s|__CLIENT_PSK__|$(cat "keys/$uid.psk")|" \
      -e "s|__SERVER_PUBKEY__|$(cat keys/server.pub)|" \
      "$conf" > "$conf.tmp"
  mv "$conf.tmp" "$conf"
done

# 4) wg0.conf 치환 — sed 스크립트를 모아 한 번에 적용한다.
echo "s|__SERVER_PRIVKEY__|$(cat keys/server.key)|" > keys/server.sed
tail -n +2 peers.csv | while IFS=, read -r uid grp; do
  [ -n "$uid" ] || continue
  {
    echo "s|__CLIENT_PUBKEY_${uid}__|$(cat "keys/$uid.pub")|"
    echo "s|__CLIENT_PSK_${uid}__|$(cat "keys/$uid.psk")|"
  } >> keys/server.sed
done
sed -f keys/server.sed wg0.conf > wg0.conf.tmp
mv wg0.conf.tmp wg0.conf

remaining=$(grep -rl '__[A-Z]' wg0.conf clients 2>/dev/null || true)
if [ -n "$remaining" ]; then
  echo "치환되지 않은 자리표시자가 남았다:" >&2
  echo "$remaining" >&2
  exit 1
fi

echo "완료. wg0.conf 는 /etc/wireguard/ 로, clients/<묶음>/<사용자>.conf 는 각 사용자에게."
echo "다음: sh fill-secrets.sh  그리고  sh iptables.sh up"
"""


def render_fill_secrets_sh(allocation: Allocation) -> str:
    """환경변수에서 프록시 비밀번호를 읽어 redsocks 설정에 채운다."""
    lines = [
        "#!/bin/sh",
        "# redsocks 설정의 비밀번호 자리표시자를 환경변수로 채운다.",
        "#",
        "#   export IPALLOC_PROXY_PASSWORD='...'        # 전 묶음 공통",
        "#   export IPALLOC_PROXY_PASSWORD_G001='...'   # 묶음별로 다르면 이쪽이 우선",
        "#   sh fill-secrets.sh",
        "#",
        "# 채운 뒤의 redsocks/*.conf 에는 평문 비밀번호가 들어간다.",
        "# 저장소에 올리거나 서버 밖으로 복사하지 말 것.",
        "set -eu",
        'cd "$(dirname "$0")"',
        "umask 077",
        "",
        "filled=0",
        "for conf in redsocks/*.conf; do",
        '  gid=$(basename "$conf" .conf)',
        '  upper=$(echo "$gid" | tr "[:lower:]" "[:upper:]")',
        '  eval "specific=\\${IPALLOC_PROXY_PASSWORD_${upper}:-}"',
        '  password="${specific:-${IPALLOC_PROXY_PASSWORD:-}}"',
        '  grep -q "__PROXY_PASSWORD_" "$conf" || continue',
        '  if [ -z "$password" ]; then',
        '    echo "환경변수가 없다: IPALLOC_PROXY_PASSWORD_${upper} 또는 IPALLOC_PROXY_PASSWORD" >&2',
        "    exit 1",
        "  fi",
        '  sed "s|__PROXY_PASSWORD_${gid}__|${password}|g" "$conf" > "$conf.tmp"',
        '  mv "$conf.tmp" "$conf"',
        "  filled=$((filled + 1))",
        "done",
        "",
        'if grep -rq "__PROXY_PASSWORD_" redsocks 2>/dev/null; then',
        '  echo "채워지지 않은 자리표시자가 남았다." >&2',
        "  exit 1",
        "fi",
        'echo "redsocks 설정 $filled 개를 채웠다."',
    ]
    return "\n".join(lines) + "\n"


def export(
    allocation: Allocation,
    proxies: list[ProxyEndpoint],
    out_dir: str | Path,
    *,
    tunnel: TunnelPlan | None = None,
    opts: ChainOptions,
    client_opts: ExportOptions | None = None,
) -> dict[str, int]:
    """프록시 경로용 서버·사용자 설정 일습.

    out_dir/
        wg0.conf              진입 호스트의 WireGuard 설정 (전 사용자 peer)
        redsocks/<묶음>.conf  묶음별 업스트림 프록시
        iptables.sh           묶음별 경로 + UDP 정책
        fill-secrets.sh       프록시 비밀번호 채우기
        genkeys.sh            WireGuard 키 생성
        peers.csv             genkeys.sh 가 읽는 목록
        clients/<묶음>/<사용자>.conf
    """
    tunnel = tunnel or TunnelPlan()
    groups = allocation.used_groups
    if len(proxies) < len(groups):
        raise ProxyChainError(f"프록시가 부족하다. 묶음 {len(groups)}개에 프록시는 {len(proxies)}개뿐이다. " f"{len(groups) - len(proxies)}개를 더 확보하거나 --per-ip 를 올릴 것.")

    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "redsocks").mkdir(exist_ok=True)

    # 사용자 설정은 전부 같은 진입 주소를 가리킨다.
    client_opts = replace(client_opts or ExportOptions(), mtu=opts.mtu)

    peers = ["user_id,group_id"]
    clients = 0
    for index, group in enumerate(groups):
        proxy = proxies[index]
        (root / "redsocks" / f"{group.id}.conf").write_text(
            render_redsocks_conf(group, opts.redsocks_port(index), proxy, salt=opts.salt), encoding="utf-8"
        )
        cdir = root / "clients" / group.id
        cdir.mkdir(parents=True, exist_ok=True)
        for m in sorted(group.members, key=lambda a: a.user_id):
            # egress_ip 는 설정 주석에만 쓰인다. 이 경로에서 출구를 정하는 것은
            # 배정표의 IP 가 아니라 프록시이므로, 낡은 IP 대신 프록시를 적는다.
            at_entry = replace(m, listen_host=opts.entry_host, listen_port=opts.entry_port, egress_ip=f"{proxy.host}:{proxy.port} 경유")
            (cdir / f"{m.user_id}.conf").write_text(render_client_conf(at_entry, group, client_opts), encoding="utf-8")
            peers.append(f"{m.user_id},{m.group_id}")
            clients += 1

    (root / "wg0.conf").write_text(render_wg_server(allocation, tunnel, opts), encoding="utf-8")
    (root / "peers.csv").write_text("\n".join(peers) + "\n", encoding="utf-8")

    for name, body in (
        ("iptables.sh", render_iptables_sh(allocation, opts)),
        ("fill-secrets.sh", render_fill_secrets_sh(allocation)),
        ("genkeys.sh", render_genkeys_sh()),
    ):
        path = root / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    return {"groups": len(groups), "clients": clients}


__all__ = [
    "DEFAULT_REDSOCKS_BASE_PORT",
    "LOCAL_DESTINATIONS",
    "ChainOptions",
    "ProxyChainError",
    "export",
    "render_fill_secrets_sh",
    "render_genkeys_sh",
    "render_iptables_sh",
    "render_redsocks_conf",
    "render_wg_server",
]
