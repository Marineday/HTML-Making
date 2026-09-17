"""CSV 입출력.

명부와 IP 목록은 대부분 엑셀에서 온다. 그래서 두 가지를 관대하게 받는다.
    - BOM (엑셀이 UTF-8 CSV 를 저장하면 앞에 붙는다)
    - 컬럼명 흔들림 (`id` / `user_id`, `ip` / `egress_ip`)

반대로 출력은 엄격하게 고정된 컬럼으로 쓴다. 이 파일이 다음 실행의 입력이
되기 때문이다.
"""

from __future__ import annotations

import csv
import ipaddress
from pathlib import Path
from typing import Iterable, Sequence

from .models import Assignment, Endpoint, Group, ModelError, User
from .pool import TunnelPlan
from .proxy import ProxyEndpoint, ProxyError

ASSIGNMENT_COLUMNS = ["user_id", "group_id", "egress_ip", "tunnel_ip", "listen_host", "listen_port", "label"]
GROUP_COLUMNS = ["group_id", "egress_ip", "subnet", "server_ip", "listen_host", "listen_port", "members", "vacancies", "region", "provider"]

_USER_ID_KEYS = ("user_id", "id", "userid", "사용자id", "사용자")
_USER_LABEL_KEYS = ("label", "name", "이름", "비고")
_IP_KEYS = ("egress_ip", "ip", "address", "출구ip")


class CsvError(ValueError):
    """CSV 읽기 오류. 어느 줄이 문제인지 항상 포함한다."""


def _rows(path: str | Path) -> list[dict[str, str]]:
    p = Path(path)
    if not p.exists():
        raise CsvError(f"파일이 없다: {p}")
    with p.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise CsvError(f"{p} 에 헤더 줄이 없다.")
        out = []
        for row in reader:
            out.append({(k or "").strip().lower(): (v or "").strip() for k, v in row.items()})
        return out


def _pick(row: dict[str, str], keys: Sequence[str]) -> str:
    for k in keys:
        if row.get(k):
            return row[k]
    return ""


def read_users(path: str | Path) -> list[User]:
    """명부를 읽는다. `user_id` (또는 `id`) 컬럼만 있으면 된다."""
    users: list[User] = []
    for n, row in enumerate(_rows(path), start=2):
        uid = _pick(row, _USER_ID_KEYS)
        if not uid:
            continue  # 엑셀에서 딸려오는 빈 줄
        try:
            users.append(User(id=uid, label=_pick(row, _USER_LABEL_KEYS)))
        except ModelError as exc:
            raise CsvError(f"{path} {n}번째 줄: {exc}") from exc
    if not users:
        raise CsvError(f"{path} 에서 사용자를 하나도 읽지 못했다. `user_id` 컬럼이 있는지 확인할 것.")
    return users


def read_endpoints(path: str | Path) -> list[Endpoint]:
    """출구 IP 목록을 읽는다. `ip` (또는 `egress_ip`) 컬럼만 있으면 된다."""
    eps: list[Endpoint] = []
    for n, row in enumerate(_rows(path), start=2):
        ip = _pick(row, _IP_KEYS)
        if not ip:
            continue
        port_raw = row.get("listen_port") or "51820"
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise CsvError(f"{path} {n}번째 줄: 포트 '{port_raw}' 는 숫자가 아니다.") from exc
        try:
            eps.append(
                Endpoint(
                    ip=ip,
                    region=row.get("region", ""),
                    provider=row.get("provider", ""),
                    listen_host=row.get("listen_host", ""),
                    listen_port=port,
                    note=row.get("note", ""),
                )
            )
        except ModelError as exc:
            raise CsvError(f"{path} {n}번째 줄: {exc}") from exc
    if not eps:
        raise CsvError(f"{path} 에서 출구 IP 를 하나도 읽지 못했다. `ip` 컬럼이 있는지 확인할 것.")
    return eps


def read_proxies(path: str | Path) -> list[ProxyEndpoint]:
    """업스트림 프록시 목록을 읽는다.

    `host` 와 `port` 만 필수다. password 에는 평문 대신 `${ENV_VAR}` 를 적는다 —
    이 함수는 그 참조를 **치환하지 않고 그대로 둔다.** 실제 값은 프록시에
    붙는 순간에만 읽는다.
    """
    out: list[ProxyEndpoint] = []
    for n, row in enumerate(_rows(path), start=2):
        host = row.get("host") or row.get("proxy_host") or ""
        if not host:
            continue
        port_raw = row.get("port") or row.get("proxy_port") or ""
        if not port_raw:
            raise CsvError(f"{path} {n}번째 줄: port 가 비어 있다.")
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise CsvError(f"{path} {n}번째 줄: 포트 '{port_raw}' 는 숫자가 아니다.") from exc
        try:
            out.append(
                ProxyEndpoint(
                    host=host,
                    port=port,
                    protocol=row.get("protocol") or "socks5",
                    username=row.get("username", ""),
                    password=row.get("password", ""),
                    provider=row.get("provider", ""),
                    region=row.get("region", ""),
                    sticky_template=row.get("sticky_template", ""),
                    note=row.get("note", ""),
                )
            )
        except ProxyError as exc:
            raise CsvError(f"{path} {n}번째 줄: {exc}") from exc
    if not out:
        raise CsvError(f"{path} 에서 프록시를 하나도 읽지 못했다. `host` 와 `port` 컬럼이 있는지 확인할 것.")
    return out


def read_assignments(path: str | Path) -> list[Assignment]:
    """기존 배정표를 읽는다. 증분 배정의 입력이다."""
    out: list[Assignment] = []
    for n, row in enumerate(_rows(path), start=2):
        uid = _pick(row, _USER_ID_KEYS)
        if not uid:
            continue
        missing = [c for c in ("group_id", "egress_ip", "tunnel_ip") if not row.get(c)]
        if missing:
            raise CsvError(f"{path} {n}번째 줄: 컬럼 {', '.join(missing)} 이(가) 비어 있다.")
        try:
            port = int(row.get("listen_port") or "51820")
        except ValueError as exc:
            raise CsvError(f"{path} {n}번째 줄: 포트가 숫자가 아니다.") from exc
        out.append(
            Assignment(
                user_id=uid,
                group_id=row["group_id"],
                egress_ip=row["egress_ip"],
                tunnel_ip=row["tunnel_ip"],
                listen_host=row.get("listen_host", "") or row["egress_ip"],
                listen_port=port,
                label=_pick(row, _USER_LABEL_KEYS),
            )
        )
    return out


def write_assignments(path: str | Path, assignments: Iterable[Assignment]) -> int:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with p.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=ASSIGNMENT_COLUMNS)
        writer.writeheader()
        for a in assignments:
            writer.writerow(a.as_row())
            count += 1
    return count


def write_groups(path: str | Path, groups: Iterable[Group], group_size: int, tunnel: TunnelPlan | None = None) -> int:
    tunnel = tunnel or TunnelPlan()
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with p.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=GROUP_COLUMNS)
        writer.writeheader()
        for g in groups:
            writer.writerow(
                {
                    "group_id": g.id,
                    "egress_ip": g.endpoint.ip,
                    "subnet": g.subnet,
                    "server_ip": tunnel.server_ip(ipaddress.ip_network(g.subnet)),
                    "listen_host": g.endpoint.listen_host,
                    "listen_port": str(g.endpoint.listen_port),
                    "members": str(g.size),
                    "vacancies": str(g.vacancies(group_size)),
                    "region": g.endpoint.region,
                    "provider": g.endpoint.provider,
                }
            )
            count += 1
    return count


def write_users(path: str | Path, users: Iterable[User]) -> int:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with p.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["user_id", "label"])
        writer.writeheader()
        for u in users:
            writer.writerow({"user_id": u.id, "label": u.label})
            count += 1
    return count


__all__ = [
    "ASSIGNMENT_COLUMNS",
    "GROUP_COLUMNS",
    "CsvError",
    "read_assignments",
    "read_endpoints",
    "read_proxies",
    "read_users",
    "write_assignments",
    "write_groups",
    "write_users",
]
