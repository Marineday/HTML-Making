"""배정표를 구성하는 값 객체들."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Iterable

# 사용자 ID. 파일명·WireGuard 인터페이스명·CSV 에 그대로 들어가므로 안전한
# 문자만 허용한다. 공백이나 슬래시가 섞이면 설정 파일을 내보낼 때 깨진다.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")

# 출구 IP 로 쓸 수 없는 대역. 여기 걸리면 서비스 쪽에서 식별이 안 되므로
# 배정표의 의미가 사라진다. ipaddress.is_private 를 그대로 쓰지 않는 이유는
# 문서용 대역(203.0.113.0/24 등)까지 막아버려 예제와 테스트를 못 쓰기 때문이다.
_UNUSABLE = (
    (ipaddress.ip_network("10.0.0.0/8"), "사설(RFC1918)"),
    (ipaddress.ip_network("172.16.0.0/12"), "사설(RFC1918)"),
    (ipaddress.ip_network("192.168.0.0/16"), "사설(RFC1918)"),
    (ipaddress.ip_network("100.64.0.0/10"), "통신사 NAT(CGNAT)"),
    (ipaddress.ip_network("127.0.0.0/8"), "루프백"),
    (ipaddress.ip_network("169.254.0.0/16"), "링크로컬"),
    (ipaddress.ip_network("224.0.0.0/4"), "멀티캐스트"),
    (ipaddress.ip_network("240.0.0.0/4"), "예약"),
    (ipaddress.ip_network("0.0.0.0/8"), "미지정"),
)

# RFC5737 문서용 대역. 문법적으로는 통과시키되 운영에 쓰면 안 된다.
_DOCUMENTATION = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
)


class ModelError(ValueError):
    """값 객체 생성 단계에서 잡히는 입력 오류."""


def validate_id(value: str, kind: str) -> str:
    value = value.strip()
    if not _SAFE_ID.match(value):
        raise ModelError(
            f"{kind} '{value}' 는 사용할 수 없다. 영숫자로 시작하고 영숫자·점·밑줄·하이픈만 " "쓸 수 있으며 63자를 넘을 수 없다."
        )
    return value


@dataclass(frozen=True)
class User:
    """배정 대상 한 명.

    label 은 사람이 알아보기 위한 이름이다. 개인정보를 넣지 않는 편이 낫다 —
    이 값은 배정표 CSV 와 WireGuard 설정 주석에 그대로 남는다.
    """

    id: str
    label: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", validate_id(self.id, "사용자 ID"))
        object.__setattr__(self, "label", self.label.strip())


@dataclass(frozen=True)
class Endpoint:
    """출구 IP 하나. 한 묶음이 이 IP 를 공유한다.

    ip
        서비스에서 보이게 될 공인 IP.
    listen_host
        사용자의 WireGuard 클라이언트가 접속할 주소. 보통 ip 와 같지만,
        도메인을 쓰거나 출구와 입구가 다른 구성이면 따로 지정한다.
    """

    ip: str
    region: str = ""
    provider: str = ""
    listen_host: str = ""
    listen_port: int = 51820
    note: str = ""

    def __post_init__(self) -> None:
        try:
            parsed = ipaddress.ip_address(self.ip.strip())
        except ValueError as exc:
            raise ModelError(f"출구 IP '{self.ip}' 를 해석할 수 없다: {exc}") from exc
        if parsed.version != 4:
            raise ModelError(f"출구 IP '{self.ip}' 는 IPv4 가 아니다. 이 도구는 IPv4 만 다룬다.")
        for net, kind in _UNUSABLE:
            if parsed in net:
                raise ModelError(f"출구 IP '{self.ip}' 는 {kind} 대역이다. 서비스 쪽에서 식별되지 않으므로 " "배정 대상이 될 수 없다.")
        if not 1 <= self.listen_port <= 65535:
            raise ModelError(f"포트 {self.listen_port} 는 범위를 벗어났다.")
        object.__setattr__(self, "ip", str(parsed))
        object.__setattr__(self, "listen_host", (self.listen_host or str(parsed)).strip())
        for f in ("region", "provider", "note"):
            object.__setattr__(self, f, getattr(self, f).strip())

    @property
    def is_documentation(self) -> bool:
        """RFC5737 예제용 주소인가. 예제·테스트는 통과하되 운영에서는 경고한다."""
        addr = ipaddress.ip_address(self.ip)
        return any(addr in net for net in _DOCUMENTATION)


@dataclass(frozen=True)
class Assignment:
    """사용자 한 명의 최종 배정 결과 — CSV 한 줄에 대응한다.

    tunnel_ip
        VPN 터널 안에서 이 사용자가 받는 사설 주소. 묶음마다 다른 서브넷을
        쓰므로 묶음이 달라지면 터널 주소도 달라진다.
    """

    user_id: str
    group_id: str
    egress_ip: str
    tunnel_ip: str
    listen_host: str
    listen_port: int
    label: str = ""

    def as_row(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "group_id": self.group_id,
            "egress_ip": self.egress_ip,
            "tunnel_ip": self.tunnel_ip,
            "listen_host": self.listen_host,
            "listen_port": str(self.listen_port),
            "label": self.label,
        }


@dataclass
class Group:
    """출구 IP 하나를 공유하는 묶음."""

    id: str
    endpoint: Endpoint
    subnet: str
    members: list[Assignment] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.members)

    def vacancies(self, capacity: int) -> int:
        return max(0, capacity - self.size)

    def used_tunnel_ips(self) -> set[str]:
        return {m.tunnel_ip for m in self.members}


@dataclass
class Allocation:
    """배정 전체 결과."""

    groups: list[Group]
    group_size: int
    unassigned: list[User] = field(default_factory=list)
    kept: int = 0
    added: int = 0
    dropped: list[str] = field(default_factory=list)
    # 쓰던 출구 IP 가 풀에서 사라져 옮겨진 사용자들. 신규와 구분해야 한다 —
    # 이 사람들에게는 설정 파일을 다시 배포해야 하기 때문이다.
    moved: list[str] = field(default_factory=list)

    @property
    def assignments(self) -> list[Assignment]:
        return [m for g in self.groups for m in g.members]

    @property
    def used_groups(self) -> list[Group]:
        return [g for g in self.groups if g.members]

    def summary(self) -> str:
        return (
            f"사용자 {len(self.assignments)}명 · 묶음 {len(self.used_groups)}개 "
            f"(묶음당 최대 {self.group_size}명) · 유지 {self.kept} · 신규 {self.added} · "
            f"이동 {len(self.moved)} · 이탈 {len(self.dropped)} · 미배정 {len(self.unassigned)}"
        )


def dedupe_users(users: Iterable[User]) -> list[User]:
    """입력 순서를 지키면서 ID 중복을 제거한다.

    같은 사람이 명부에 두 번 들어오면 묶음 정원이 조용히 깎인다. 배정 전에
    제거하되, 중복은 호출자가 알 수 있도록 개수를 함께 돌려준다.
    """
    seen: set[str] = set()
    out: list[User] = []
    for u in users:
        if u.id in seen:
            continue
        seen.add(u.id)
        out.append(u)
    return out
