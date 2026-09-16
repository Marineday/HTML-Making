"""배정표를 WireGuard 설정으로 내보낸다.

왜 WireGuard 인가
    사용자 접근성이 요구사항이다. WireGuard 는 윈도우·맥·iOS·안드로이드에
    공식 클라이언트가 있고, 설정 파일 하나(또는 QR 코드 하나)면 끝난다.
    OpenVPN 처럼 인증서를 발급하고 배포할 필요가 없다.

키는 여기서 만들지 않는다
    파이썬 표준 라이브러리에는 X25519 키 생성이 없다. 직접 구현하는 것은
    나쁜 생각이다. 대신 설정 파일에 자리표시자를 넣고, `wg genkey` 로 키를
    만들어 채워 넣는 스크립트(genkeys.sh)를 같이 내보낸다.
    **genkeys.sh 를 돌리기 전의 설정 파일은 동작하지 않는다. 그게 의도다.**

전체 터널 vs 분할 터널 (AllowedIPs)
    기본값은 전체 터널(0.0.0.0/0)이다. 사용자의 모든 트래픽이 출구 IP 로
    나가므로 IP 통일은 확실하다. 대신 비용이 따라온다 — 영상 20분만이 아니라
    그 사람이 VPN 을 켜둔 동안의 모든 트래픽이 회선을 지나간다. capacity 모듈의
    전송량 추정(월 20분 기준)은 이때 의미를 잃는다.

    서비스 대역만 터널로 보내면(예: `--allowed-ips 203.0.113.0/24`) 전송량
    추정이 그대로 맞고 사용자 체감 속도도 낫다. 서비스 IP 대역이 고정이라면
    분할 터널을 쓸 것.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path

from .models import Allocation, Assignment, Group
from .pool import TunnelPlan

DEFAULT_DNS = "1.1.1.1"
DEFAULT_ALLOWED_IPS = "0.0.0.0/0"
DEFAULT_WAN_INTERFACE = "eth0"
# 1420 = 1500(이더넷) - 80(WireGuard 오버헤드). 모바일 회선에서 조각화를 막는다.
DEFAULT_MTU = 1420


@dataclass(frozen=True)
class ExportOptions:
    dns: str = DEFAULT_DNS
    allowed_ips: str = DEFAULT_ALLOWED_IPS
    wan_interface: str = DEFAULT_WAN_INTERFACE
    mtu: int = DEFAULT_MTU
    keepalive: int = 25


def render_server_conf(group: Group, tunnel: TunnelPlan, opts: ExportOptions) -> str:
    """묶음 하나의 서버 설정.

    핵심은 PostUp 의 SNAT 한 줄이다. 한 호스트에 여러 공인 IP 가 붙어 있을 때,
    이 규칙이 없으면 모든 묶음이 호스트의 기본 IP 로 나가서 배정표가 무의미해진다.
    """
    subnet = ipaddress.ip_network(group.subnet)
    server_ip = tunnel.server_ip(subnet)
    ep = group.endpoint

    lines = [
        f"# 묶음 {group.id} — 출구 IP {ep.ip}"
        + (f" ({ep.region})" if ep.region else "")
        + f", 인원 {group.size}명",
        "# genkeys.sh 를 먼저 돌려야 동작한다.",
        "",
        "[Interface]",
        f"Address = {server_ip}/{subnet.prefixlen}",
        f"ListenPort = {ep.listen_port}",
        "PrivateKey = __SERVER_PRIVKEY__",
        f"MTU = {opts.mtu}",
        "",
        "# 이 묶음의 트래픽만 지정된 출구 IP 로 내보낸다.",
        f"PostUp = iptables -t nat -A POSTROUTING -s {group.subnet} -o {opts.wan_interface} -j SNAT --to-source {ep.ip}",
        "PostUp = iptables -A FORWARD -i %i -j ACCEPT",
        "PostUp = iptables -A FORWARD -o %i -j ACCEPT",
        f"PostDown = iptables -t nat -D POSTROUTING -s {group.subnet} -o {opts.wan_interface} -j SNAT --to-source {ep.ip}",
        "PostDown = iptables -D FORWARD -i %i -j ACCEPT",
        "PostDown = iptables -D FORWARD -o %i -j ACCEPT",
    ]

    for m in sorted(group.members, key=lambda a: a.user_id):
        lines += [
            "",
            f"[Peer]  # {m.user_id}" + (f" ({m.label})" if m.label else ""),
            f"PublicKey = __CLIENT_PUBKEY_{m.user_id}__",
            f"PresharedKey = __CLIENT_PSK_{m.user_id}__",
            f"AllowedIPs = {m.tunnel_ip}/32",
        ]
    return "\n".join(lines) + "\n"


def render_client_conf(assignment: Assignment, group: Group, opts: ExportOptions) -> str:
    """사용자 한 명에게 전달할 설정. 이 파일 하나면 접속이 된다."""
    subnet = ipaddress.ip_network(group.subnet)
    lines = [
        f"# {assignment.user_id}" + (f" ({assignment.label})" if assignment.label else ""),
        f"# 묶음 {assignment.group_id} · 출구 IP {assignment.egress_ip}",
        "",
        "[Interface]",
        "PrivateKey = __CLIENT_PRIVKEY__",
        f"Address = {assignment.tunnel_ip}/{subnet.max_prefixlen}",
        f"DNS = {opts.dns}",
        f"MTU = {opts.mtu}",
        "",
        "[Peer]",
        "PublicKey = __SERVER_PUBKEY__",
        "PresharedKey = __CLIENT_PSK__",
        f"AllowedIPs = {opts.allowed_ips}",
        f"Endpoint = {assignment.listen_host}:{assignment.listen_port}",
        f"PersistentKeepalive = {opts.keepalive}",
    ]
    return "\n".join(lines) + "\n"


def render_genkeys_sh() -> str:
    """키를 만들고 자리표시자를 채우는 스크립트.

    peers.csv 를 읽어 돌기 때문에, 사용자가 늘어도 스크립트는 그대로다.
    이미 있는 키는 다시 만들지 않는다 — 두 번 돌려도 기존 사용자의 설정이
    무효가 되지 않는다.
    """
    return r"""#!/bin/sh
# WireGuard 키 생성 + 설정 파일의 자리표시자 치환.
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

groups=$(tail -n +2 peers.csv | cut -d, -f2 | sort -u)

# 1) 묶음 서버 키
for g in $groups; do
  [ -s "keys/$g.key" ] || wg genkey > "keys/$g.key"
  wg pubkey < "keys/$g.key" > "keys/$g.pub"
done

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
  grep -q '__CLIENT_PRIVKEY__' "$conf" || continue   # 이미 채워진 파일은 건너뛴다
  sed -e "s|__CLIENT_PRIVKEY__|$(cat "keys/$uid.key")|" \
      -e "s|__CLIENT_PSK__|$(cat "keys/$uid.psk")|" \
      -e "s|__SERVER_PUBKEY__|$(cat "keys/$grp.pub")|" \
      "$conf" > "$conf.tmp"
  mv "$conf.tmp" "$conf"
done

# 4) 서버 설정 치환 — 묶음마다 sed 스크립트를 모아 한 번에 적용한다.
for g in $groups; do
  echo "s|__SERVER_PRIVKEY__|$(cat "keys/$g.key")|" > "keys/$g.sed"
done
tail -n +2 peers.csv | while IFS=, read -r uid grp; do
  [ -n "$uid" ] || continue
  {
    echo "s|__CLIENT_PUBKEY_${uid}__|$(cat "keys/$uid.pub")|"
    echo "s|__CLIENT_PSK_${uid}__|$(cat "keys/$uid.psk")|"
  } >> "keys/$grp.sed"
done
for g in $groups; do
  conf="server/$g.conf"
  [ -f "$conf" ] || { echo "설정 파일이 없다: $conf" >&2; exit 1; }
  sed -f "keys/$g.sed" "$conf" > "$conf.tmp"
  mv "$conf.tmp" "$conf"
done

remaining=$(grep -rl '__[A-Z]' server clients 2>/dev/null || true)
if [ -n "$remaining" ]; then
  echo "치환되지 않은 자리표시자가 남았다:" >&2
  echo "$remaining" >&2
  exit 1
fi

echo "완료. server/ 는 서버에, clients/<묶음>/<사용자>.conf 는 각 사용자에게 전달할 것."
echo "QR 코드가 필요하면: qrencode -t ansiutf8 < clients/g001/u0001.conf"
"""


def export(
    allocation: Allocation,
    out_dir: str | Path,
    *,
    tunnel: TunnelPlan | None = None,
    opts: ExportOptions | None = None,
) -> dict[str, int]:
    """설정 일습을 디렉터리로 내보낸다.

    out_dir/
        server/<묶음>.conf      서버에 올릴 설정
        clients/<묶음>/<사용자>.conf   사용자에게 줄 설정
        peers.csv               genkeys.sh 가 읽는 목록
        genkeys.sh              키 생성 + 치환
    """
    tunnel = tunnel or TunnelPlan()
    opts = opts or ExportOptions()
    root = Path(out_dir)
    (root / "server").mkdir(parents=True, exist_ok=True)

    peers: list[str] = ["user_id,group_id"]
    servers = 0
    clients = 0
    for group in allocation.used_groups:
        (root / "server" / f"{group.id}.conf").write_text(render_server_conf(group, tunnel, opts), encoding="utf-8")
        servers += 1
        cdir = root / "clients" / group.id
        cdir.mkdir(parents=True, exist_ok=True)
        for m in sorted(group.members, key=lambda a: a.user_id):
            (cdir / f"{m.user_id}.conf").write_text(render_client_conf(m, group, opts), encoding="utf-8")
            peers.append(f"{m.user_id},{m.group_id}")
            clients += 1

    (root / "peers.csv").write_text("\n".join(peers) + "\n", encoding="utf-8")
    script = root / "genkeys.sh"
    script.write_text(render_genkeys_sh(), encoding="utf-8")
    script.chmod(0o755)
    return {"servers": servers, "clients": clients}


__all__ = [
    "DEFAULT_ALLOWED_IPS",
    "DEFAULT_DNS",
    "DEFAULT_MTU",
    "DEFAULT_WAN_INTERFACE",
    "ExportOptions",
    "export",
    "render_client_conf",
    "render_genkeys_sh",
    "render_server_conf",
]
