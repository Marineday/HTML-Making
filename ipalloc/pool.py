"""출구 IP 풀과 터널 서브넷 분할.

출구 IP 는 두 가지 방법으로 준다.
    1. CSV — 공급사에서 받은 IP 목록을 그대로 (권장).
    2. CIDR — `203.0.113.0/28` 처럼 연속 대역을 받았을 때.

터널 서브넷은 묶음마다 하나씩 잘라 쓴다. 묶음이 서로 다른 사설 대역을
쓰므로, 나중에 묶음을 합치거나 서버를 옮겨도 주소가 충돌하지 않는다.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Iterator, Sequence

from .models import Endpoint

# 묶음당 터널 서브넷 크기. /24 면 호스트 254개라 묶음 정원 20명에 여유가 크다.
DEFAULT_TUNNEL_CIDR = "10.77.0.0/16"
DEFAULT_GROUP_PREFIX = 24

# 터널에 쓸 수 있는 대역은 RFC1918 뿐이다. ipaddress.is_private 는 문서용
# 대역(203.0.113.0/24 등)까지 사설로 치기 때문에 그대로 쓰면 공인 대역을
# 터널 주소로 잡는 실수를 잡아내지 못한다.
_RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


class PoolError(ValueError):
    """IP 풀 구성 오류."""


def endpoints_from_cidr(
    cidr: str,
    *,
    region: str = "",
    provider: str = "",
    listen_port: int = 51820,
    limit: int | None = None,
) -> list[Endpoint]:
    """CIDR 대역을 출구 IP 목록으로 편다.

    네트워크 주소와 브로드캐스트 주소는 뺀다 — 공급사가 /28 을 줬다고 16개를
    다 쓸 수 있는 게 아니다. 보통 쓸 수 있는 건 14개다. 이걸 모르고 용량을
    계산하면 마지막 묶음이 갈 곳을 잃는다.
    """
    try:
        net = ipaddress.ip_network(cidr.strip(), strict=False)
    except ValueError as exc:
        raise PoolError(f"CIDR '{cidr}' 를 해석할 수 없다: {exc}") from exc
    if net.version != 4:
        raise PoolError("이 도구는 IPv4 대역만 다룬다.")
    if net.num_addresses > 4096:
        raise PoolError(f"대역 {cidr} 가 너무 크다({net.num_addresses}개). 필요한 만큼만 지정할 것.")

    hosts = list(net.hosts()) if net.prefixlen < 31 else list(net)
    out: list[Endpoint] = []
    for addr in hosts:
        if limit is not None and len(out) >= limit:
            break
        out.append(
            Endpoint(
                ip=str(addr),
                region=region,
                provider=provider,
                listen_port=listen_port,
                note=f"from {cidr}",
            )
        )
    if not out:
        raise PoolError(f"대역 {cidr} 에서 쓸 수 있는 주소가 없다.")
    return out


def dedupe_endpoints(endpoints: Sequence[Endpoint]) -> list[Endpoint]:
    """같은 IP 가 두 번 들어오면 두 묶음이 한 IP 를 쓰게 된다. 순서를 지키며 제거한다."""
    seen: set[str] = set()
    out: list[Endpoint] = []
    for ep in endpoints:
        if ep.ip in seen:
            continue
        seen.add(ep.ip)
        out.append(ep)
    return out


@dataclass(frozen=True)
class TunnelPlan:
    """묶음별 터널 서브넷을 잘라내는 계획."""

    base_cidr: str = DEFAULT_TUNNEL_CIDR
    group_prefix: int = DEFAULT_GROUP_PREFIX

    def __post_init__(self) -> None:
        try:
            net = ipaddress.ip_network(self.base_cidr, strict=False)
        except ValueError as exc:
            raise PoolError(f"터널 대역 '{self.base_cidr}' 를 해석할 수 없다: {exc}") from exc
        if net.version != 4:
            raise PoolError("터널 대역은 IPv4 여야 한다.")
        if not any(net.subnet_of(private) for private in _RFC1918):
            raise PoolError(f"터널 대역 {self.base_cidr} 가 사설 대역이 아니다. " "RFC1918 (10/8, 172.16/12, 192.168/16) 안에서 골라야 한다.")
        if not net.prefixlen <= self.group_prefix <= 30:
            raise PoolError(f"묶음 프리픽스 /{self.group_prefix} 가 터널 대역 /{net.prefixlen} 과 맞지 않는다.")

    @property
    def capacity(self) -> int:
        """이 계획으로 만들 수 있는 묶음 수."""
        net = ipaddress.ip_network(self.base_cidr, strict=False)
        return 1 << (self.group_prefix - net.prefixlen)

    @property
    def hosts_per_group(self) -> int:
        """묶음 하나가 수용하는 인원. .1 은 서버가 쓰므로 하나 뺀다."""
        return (1 << (32 - self.group_prefix)) - 3

    def subnets(self) -> Iterator[ipaddress.IPv4Network]:
        net = ipaddress.ip_network(self.base_cidr, strict=False)
        yield from net.subnets(new_prefix=self.group_prefix)

    def subnet_for(self, index: int) -> ipaddress.IPv4Network:
        if not 0 <= index < self.capacity:
            raise PoolError(f"{index + 1}번째 묶음은 터널 대역 {self.base_cidr} 안에 들어가지 않는다. " f"수용 가능 묶음은 {self.capacity}개다.")
        net = ipaddress.ip_network(self.base_cidr, strict=False)
        offset = index << (32 - self.group_prefix)
        return ipaddress.ip_network((int(net.network_address) + offset, self.group_prefix))

    def server_ip(self, subnet: ipaddress.IPv4Network) -> str:
        """묶음 서버(WireGuard 인터페이스)가 쓰는 주소 — 서브넷의 첫 호스트."""
        return str(next(subnet.hosts()))

    def member_ips(self, subnet: ipaddress.IPv4Network) -> Iterator[str]:
        """사용자에게 줄 수 있는 주소들. 서버 주소 다음부터."""
        hosts = subnet.hosts()
        next(hosts, None)  # 서버 몫
        for addr in hosts:
            yield str(addr)


def required_groups(user_count: int, group_size: int) -> int:
    """인원과 묶음 정원으로 필요한 출구 IP 개수를 구한다."""
    if group_size < 1:
        raise PoolError("묶음 정원은 1 이상이어야 한다.")
    if user_count < 0:
        raise PoolError("인원은 0 이상이어야 한다.")
    return -(-user_count // group_size)


def check_pool(user_count: int, group_size: int, endpoint_count: int) -> None:
    """배정 전에 IP 가 모자라지 않는지 본다. 모자라면 몇 개가 더 필요한지 말한다."""
    need = required_groups(user_count, group_size)
    if endpoint_count < need:
        raise PoolError(
            f"출구 IP 가 부족하다. 사용자 {user_count}명을 {group_size}명씩 묶으면 "
            f"{need}개가 필요한데 {endpoint_count}개뿐이다. {need - endpoint_count}개를 더 확보하거나 "
            f"묶음 정원을 {-(-user_count // max(1, endpoint_count))}명으로 올려야 한다."
        )


__all__ = [
    "DEFAULT_GROUP_PREFIX",
    "DEFAULT_TUNNEL_CIDR",
    "PoolError",
    "TunnelPlan",
    "check_pool",
    "dedupe_endpoints",
    "endpoints_from_cidr",
    "required_groups",
]
