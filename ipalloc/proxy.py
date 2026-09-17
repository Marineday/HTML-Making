"""레지덴셜/ISP 프록시 업스트림.

구조가 데이터센터 방식과 다르다
    프록시는 HTTP/SOCKS 엔드포인트라 WireGuard 서버를 띄울 수도, 사용자가
    인바운드로 붙을 수도 없다. 그래서 체인이 이렇게 된다.

        사용자 → WireGuard 진입 호스트 → 묶음별 프록시 → 서비스

    대신 **진입 IP 는 묶음 수만큼 필요하지 않다.** 출구를 구분하는 것은
    프록시이지 진입 IP 가 아니므로, 진입 호스트는 1~2대(이중화용)면 된다.

"고정 IP" 가 아니라 "sticky" 다
    레지덴셜 풀은 로테이션이 기본이다. 공급사는 보통 사용자명에 세션 토큰을
    끼워 넣으면 일정 시간 같은 출구를 준다(sticky session). 그 시간은 대개
    10~30분이고 **보장이 아니다.** 이 모듈은 묶음마다 결정적인 세션 토큰을
    만들어 같은 묶음이 늘 같은 토큰을 요청하게 하지만, 공급사가 그 토큰을
    얼마나 지켜주는지는 proxy-check 로 직접 확인해야 한다.

비밀번호는 생성물에 넣지 않는다
    CSV 의 password 컬럼에는 `${ENV_VAR}` 를 적는다. 이 모듈은 그 참조를
    **그대로 들고 다니고**, 실제 값이 필요한 순간(프록시에 붙을 때)에만
    환경변수에서 읽는다. 생성되는 redsocks 설정에는 자리표시자만 들어가고,
    서버에서 fill-secrets.sh 가 환경변수로 채운다 — 설정 파일을 저장소나
    배포 아티팩트에 두어도 비밀번호가 따라가지 않는다.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

PROTOCOLS = ("socks5", "http")

# 공급사마다 sticky 세션을 넣는 자리가 다르다. 대부분 사용자명에 끼워 넣는다.
#   user-session-abc123 / user_session-abc123 / customer-zone-kr-session-abc123
# {username} 과 {session} 을 치환한다.
DEFAULT_STICKY_TEMPLATE = "{username}-session-{session}"
SESSION_ID_LENGTH = 10


class ProxyError(ValueError):
    """프록시 설정 오류."""


def expand_env(value: str, *, field: str) -> str:
    """${ENV_VAR} 를 환경변수로 치환한다. 없으면 즉시 실패시킨다."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ProxyError(f"환경변수 {name} 가 설정되지 않았다 ({field} 가 ${{{name}}} 를 참조함).")
        return os.environ[name]

    return _ENV_REF.sub(replace, value)


@dataclass(frozen=True)
class ProxyEndpoint:
    """업스트림 프록시 하나. 묶음 하나가 이걸 쓴다."""

    host: str
    port: int
    protocol: str = "socks5"
    username: str = ""
    password: str = ""
    provider: str = ""
    region: str = ""
    sticky_template: str = DEFAULT_STICKY_TEMPLATE
    note: str = ""

    def __post_init__(self) -> None:
        host = self.host.strip()
        if not host:
            raise ProxyError("프록시 host 가 비어 있다.")
        if any(c.isspace() for c in host):
            raise ProxyError(f"프록시 host '{host}' 에 공백이 있다.")
        object.__setattr__(self, "host", host)

        if not 1 <= self.port <= 65535:
            raise ProxyError(f"프록시 포트 {self.port} 는 범위를 벗어났다.")

        protocol = self.protocol.strip().lower()
        if protocol not in PROTOCOLS:
            raise ProxyError(f"프록시 protocol '{self.protocol}' 을 모른다. {' / '.join(PROTOCOLS)} 중 하나여야 한다.")
        object.__setattr__(self, "protocol", protocol)

        for field in ("username", "password", "provider", "region", "note"):
            object.__setattr__(self, field, getattr(self, field).strip())

        template = self.sticky_template.strip() or DEFAULT_STICKY_TEMPLATE
        if "{session}" not in template:
            raise ProxyError(f"sticky_template '{template}' 에 {{session}} 자리가 없다. " "세션 토큰을 넣을 곳이 없으면 묶음마다 출구가 갈린다.")
        object.__setattr__(self, "sticky_template", template)

    @property
    def needs_auth(self) -> bool:
        return bool(self.username or self.password)

    @property
    def password_env_var(self) -> str | None:
        """password 가 통째로 ${VAR} 참조면 그 변수명. 아니면 None.

        None 이 나온다는 것은 평문 비밀번호가 CSV 에 적혀 있다는 뜻이다.
        호출자는 그걸 경고해야 한다.
        """
        m = _ENV_REF.fullmatch(self.password.strip())
        return m.group(1) if m else None

    def resolve_password(self) -> str:
        """실제 비밀번호. 프록시에 붙는 순간에만 부른다."""
        return expand_env(self.password, field=f"{self.host}:{self.port} 의 password")

    def redacted(self) -> str:
        """로그와 화면에 찍기 위한 표현. 비밀번호는 절대 포함하지 않는다."""
        who = f"{self.username}@" if self.username else ""
        return f"{self.protocol}://{who}{self.host}:{self.port}"


def session_id(group_id: str, *, salt: str = "") -> str:
    """묶음마다 결정적인 sticky 세션 토큰.

    같은 묶음은 언제 실행해도 같은 토큰을 요청한다. 실행할 때마다 난수를
    쓰면 재시작마다 출구가 갈려 "묶음은 같은 IP" 전제가 매번 깨진다.

    salt 를 바꾸면 모든 묶음의 세션이 한꺼번에 갈린다 — 출구를 통째로
    갈아야 할 때 쓰는 손잡이다.
    """
    digest = hashlib.sha256(f"{salt}|{group_id}".encode()).hexdigest()
    return digest[:SESSION_ID_LENGTH]


def sticky_username(proxy: ProxyEndpoint, group_id: str, *, salt: str = "") -> str:
    """이 묶음이 프록시에 제시할 사용자명.

    username 이 비어 있으면 인증 없는 프록시이므로 그대로 빈 문자열을 돌려준다.
    """
    if not proxy.username:
        return ""
    return proxy.sticky_template.format(username=proxy.username, session=session_id(group_id, salt=salt))


def dedupe_proxies(proxies: list[ProxyEndpoint]) -> list[ProxyEndpoint]:
    """같은 host:port:username 이 두 번 들어오면 두 묶음이 한 출구를 공유한다."""
    seen: set[tuple[str, int, str]] = set()
    out: list[ProxyEndpoint] = []
    for p in proxies:
        key = (p.host, p.port, p.username)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


__all__ = [
    "DEFAULT_STICKY_TEMPLATE",
    "PROTOCOLS",
    "ProxyEndpoint",
    "ProxyError",
    "dedupe_proxies",
    "expand_env",
    "session_id",
    "sticky_username",
]
