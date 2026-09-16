"""사용자를 묶고, 묶음에 출구 IP 를 붙인다.

핵심 규칙은 하나다. **이미 배정된 사람은 움직이지 않는다.**

배정표는 한 번 만들고 끝나지 않는다. 사람이 빠지고 들어온다. 매번 처음부터
다시 묶으면 아무 상관 없는 사용자의 출구 IP 가 바뀌고, "한 묶음은 늘 같은
IP" 라는 전제가 깨진다. 그래서 allocate() 는 기존 배정표를 입력으로 받아
빈자리부터 채운다.

사람이 옮겨지는 경우는 하나뿐이다 — 그 사람이 쓰던 출구 IP 가 풀에서
사라졌을 때. 이때만 재배정하고, 결과에 따로 표시한다.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Iterable, Sequence

from .models import Allocation, Assignment, Endpoint, Group, User, dedupe_users
from .pool import TunnelPlan, dedupe_endpoints, required_groups

_GROUP_ID = re.compile(r"^g(\d+)$")

DEFAULT_GROUP_SIZE = 20


class AllocationError(ValueError):
    """배정 자체가 불가능할 때."""


def group_id_for(index: int) -> str:
    """서브넷 인덱스(0-based)를 묶음 ID 로. g001 은 터널 대역의 첫 서브넷이다."""
    return f"g{index + 1:03d}"


def index_of(group_id: str) -> int:
    m = _GROUP_ID.match(group_id.strip())
    if not m:
        raise AllocationError(f"묶음 ID '{group_id}' 형식이 아니다. g001 처럼 써야 한다.")
    idx = int(m.group(1)) - 1
    if idx < 0:
        raise AllocationError(f"묶음 ID '{group_id}' 는 g001 부터 시작해야 한다.")
    return idx


def allocate(
    users: Sequence[User],
    endpoints: Sequence[Endpoint],
    *,
    group_size: int = DEFAULT_GROUP_SIZE,
    tunnel: TunnelPlan | None = None,
    existing: Iterable[Assignment] = (),
) -> Allocation:
    """사용자 명부와 출구 IP 풀로 배정표를 만든다.

    existing 을 주면 증분 배정이 된다. 주지 않으면 처음부터 배정한다.
    """
    tunnel = tunnel or TunnelPlan()
    if group_size < 1:
        raise AllocationError("묶음 정원은 1 이상이어야 한다.")
    if group_size > tunnel.hosts_per_group:
        raise AllocationError(
            f"묶음 정원 {group_size}명은 터널 서브넷 /{tunnel.group_prefix} 에 들어가지 않는다. "
            f"이 프리픽스로는 {tunnel.hosts_per_group}명이 최대다."
        )

    users = dedupe_users(users)
    endpoints = dedupe_endpoints(endpoints)
    if not endpoints:
        raise AllocationError("출구 IP 풀이 비어 있다.")

    by_ip = {ep.ip: ep for ep in endpoints}
    user_by_id = {u.id: u for u in users}
    prior = [a for a in existing]

    # 1) 기존 배정표에서 (출구 IP -> 묶음 ID) 를 되살린다. 풀에서 사라진 IP 는 버린다.
    ip_to_group: dict[str, str] = {}
    taken_index: set[int] = set()
    for a in prior:
        if a.egress_ip not in by_ip:
            continue
        current = ip_to_group.get(a.egress_ip)
        if current and current != a.group_id:
            raise AllocationError(f"기존 배정표가 깨져 있다. 출구 IP {a.egress_ip} 에 묶음 " f"{current} 와 {a.group_id} 가 동시에 붙어 있다.")
        if current:
            continue
        idx = index_of(a.group_id)
        if idx in taken_index:
            raise AllocationError(f"기존 배정표가 깨져 있다. 묶음 {a.group_id} 가 두 개의 출구 IP 에 붙어 있다.")
        taken_index.add(idx)
        ip_to_group[a.egress_ip] = a.group_id

    # 2) 새 출구 IP 에 아직 안 쓰인 서브넷 인덱스를 낮은 것부터 준다.
    next_index = 0
    for ep in endpoints:
        if ep.ip in ip_to_group:
            continue
        while next_index in taken_index:
            next_index += 1
        if next_index >= tunnel.capacity:
            raise AllocationError(f"터널 대역 {tunnel.base_cidr} 로는 묶음을 {tunnel.capacity}개까지만 만들 수 있다. " f"출구 IP 는 {len(endpoints)}개다. 더 큰 사설 대역을 쓸 것.")
        taken_index.add(next_index)
        ip_to_group[ep.ip] = group_id_for(next_index)

    # 3) 묶음 뼈대. 묶음 ID 순으로 정렬해 빈자리 채우는 순서를 결정적으로 만든다.
    groups: dict[str, Group] = {}
    for ep in endpoints:
        gid = ip_to_group[ep.ip]
        subnet = tunnel.subnet_for(index_of(gid))
        groups[gid] = Group(id=gid, endpoint=ep, subnet=str(subnet))
    order = sorted(groups, key=index_of)

    # 4) 기존 배정 유지. 출구 IP 가 살아 있고 명부에도 남아 있는 사람만.
    kept = 0
    remapped: list[User] = []
    seen_users: set[str] = set()
    for a in prior:
        if a.user_id in seen_users or a.user_id not in user_by_id:
            continue
        seen_users.add(a.user_id)
        user = user_by_id[a.user_id]
        if a.egress_ip not in by_ip:
            remapped.append(user)  # 쓰던 IP 가 사라졌다. 아래에서 새로 배정한다.
            continue
        gid = ip_to_group[a.egress_ip]
        ep = by_ip[a.egress_ip]
        group = groups[gid]
        tunnel_ip = a.tunnel_ip if _in_subnet(a.tunnel_ip, group.subnet) else _next_tunnel_ip(group, tunnel)
        if tunnel_ip in group.used_tunnel_ips():
            tunnel_ip = _next_tunnel_ip(group, tunnel)
        group.members.append(
            Assignment(
                user_id=user.id,
                group_id=gid,
                egress_ip=ep.ip,
                tunnel_ip=tunnel_ip,
                listen_host=ep.listen_host,
                listen_port=ep.listen_port,
                label=user.label,
            )
        )
        kept += 1

    dropped = sorted(a.user_id for a in prior if a.user_id not in user_by_id)

    # 5) 신규 사용자 + 재배정 대상. 명부 순서를 지켜 낮은 묶음의 빈자리부터.
    pending = remapped + [u for u in users if u.id not in seen_users]
    unassigned: list[User] = []
    cursor = 0
    for user in pending:
        placed = False
        while cursor < len(order):
            group = groups[order[cursor]]
            if group.vacancies(group_size) <= 0:
                cursor += 1
                continue
            group.members.append(
                Assignment(
                    user_id=user.id,
                    group_id=group.id,
                    egress_ip=group.endpoint.ip,
                    tunnel_ip=_next_tunnel_ip(group, tunnel),
                    listen_host=group.endpoint.listen_host,
                    listen_port=group.endpoint.listen_port,
                    label=user.label,
                )
            )
            placed = True
            break
        if not placed:
            unassigned.append(user)

    # 재배정된 사용자를 신규로 세면 안 된다. 신규는 설정을 처음 받는 사람이고,
    # 재배정된 사람은 이미 받은 설정을 버리고 다시 받아야 하는 사람이다.
    remapped_ids = {u.id for u in remapped}
    unassigned_ids = {u.id for u in unassigned}
    return Allocation(
        groups=[groups[gid] for gid in order],
        group_size=group_size,
        unassigned=unassigned,
        kept=kept,
        added=sum(1 for u in pending if u.id not in remapped_ids and u.id not in unassigned_ids),
        dropped=dropped,
        moved=[u.id for u in remapped if u.id not in unassigned_ids],
    )


def rebuild(
    assignments: Iterable[Assignment],
    *,
    group_size: int = DEFAULT_GROUP_SIZE,
    tunnel: TunnelPlan | None = None,
) -> Allocation:
    """배정표 CSV 만으로 Allocation 을 복원한다.

    verify / 설정 내보내기는 IP 풀 원본 없이도 돌아야 한다. 운영 중에는
    배정표 CSV 가 사실상의 원본이기 때문이다.
    """
    tunnel = tunnel or TunnelPlan()
    groups: dict[str, Group] = {}
    for a in assignments:
        group = groups.get(a.group_id)
        if group is None:
            endpoint = Endpoint(
                ip=a.egress_ip,
                listen_host=a.listen_host or a.egress_ip,
                listen_port=a.listen_port,
            )
            group = Group(
                id=a.group_id,
                endpoint=endpoint,
                subnet=str(tunnel.subnet_for(index_of(a.group_id))),
            )
            groups[a.group_id] = group
        group.members.append(a)
    order = sorted(groups, key=index_of)
    return Allocation(
        groups=[groups[g] for g in order],
        group_size=group_size,
        kept=sum(len(groups[g].members) for g in order),
    )


def _in_subnet(ip: str, subnet: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(subnet)
    except ValueError:
        return False


def _next_tunnel_ip(group: Group, tunnel: TunnelPlan) -> str:
    used = group.used_tunnel_ips()
    for candidate in tunnel.member_ips(ipaddress.ip_network(group.subnet)):
        if candidate not in used:
            return candidate
    raise AllocationError(f"묶음 {group.id} 의 터널 서브넷 {group.subnet} 에 빈 주소가 없다.")


def verify(allocation: Allocation) -> list[str]:
    """배정표가 지켜야 할 불변식을 검사한다. 문제를 사람이 읽는 문장으로 돌려준다.

    배정표는 사람이 손으로 고치게 되어 있다 — 그래서 고친 뒤 이 검사를
    돌릴 수 있어야 한다. 빈 목록이 나오면 정상이다.
    """
    problems: list[str] = []
    seen_user: dict[str, str] = {}
    seen_ip: dict[str, str] = {}
    seen_tunnel: dict[str, str] = {}

    for group in allocation.groups:
        if group.size > allocation.group_size:
            problems.append(f"묶음 {group.id} 정원 초과: {group.size}명 (최대 {allocation.group_size}명)")
        owner = seen_ip.get(group.endpoint.ip)
        if owner and group.members:
            problems.append(f"출구 IP {group.endpoint.ip} 를 묶음 {owner} 와 {group.id} 가 같이 쓴다")
        elif group.members:
            seen_ip[group.endpoint.ip] = group.id

        for m in group.members:
            if m.group_id != group.id:
                problems.append(f"사용자 {m.user_id} 의 묶음 ID({m.group_id})가 소속 묶음({group.id})과 다르다")
            if m.egress_ip != group.endpoint.ip:
                problems.append(f"사용자 {m.user_id} 의 출구 IP({m.egress_ip})가 묶음 {group.id} 의 " f"IP({group.endpoint.ip})와 다르다")
            if not _in_subnet(m.tunnel_ip, group.subnet):
                problems.append(f"사용자 {m.user_id} 의 터널 주소 {m.tunnel_ip} 가 묶음 서브넷 {group.subnet} 밖이다")
            prev = seen_user.get(m.user_id)
            if prev:
                problems.append(f"사용자 {m.user_id} 가 묶음 {prev} 와 {group.id} 에 중복 배정됐다")
            else:
                seen_user[m.user_id] = group.id
            prev_t = seen_tunnel.get(m.tunnel_ip)
            if prev_t:
                problems.append(f"터널 주소 {m.tunnel_ip} 가 {prev_t} 와 {m.user_id} 에 중복 배정됐다")
            else:
                seen_tunnel[m.tunnel_ip] = m.user_id

    if allocation.unassigned:
        need = required_groups(len(allocation.assignments) + len(allocation.unassigned), allocation.group_size)
        problems.append(f"미배정 {len(allocation.unassigned)}명 — 출구 IP 가 총 {need}개 필요하다")
    return problems


__all__ = [
    "DEFAULT_GROUP_SIZE",
    "AllocationError",
    "allocate",
    "group_id_for",
    "index_of",
    "rebuild",
    "verify",
]
