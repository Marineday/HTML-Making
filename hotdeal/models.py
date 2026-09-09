"""파이프라인을 흐르는 값 객체들."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 링크 추적용 파라미터. 같은 딜이 소스마다 다른 utm 을 달고 오므로 제거해야
# 중복제거가 동작한다.
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "fbclid",
        "gclid",
        "igshid",
        "spm",
        "ref",
        "referrer",
        "from",
    }
)

_NON_WORD = re.compile(r"[^0-9a-z가-힣]+")
_PRICE_PATTERNS = (
    # "(39,900원/무료)", "39900원", "$12.99"
    re.compile(r"([0-9][0-9,]{2,})\s*원"),
    re.compile(r"₩\s*([0-9][0-9,]{2,})"),
)


def normalize_url(url: str) -> str:
    """추적 파라미터와 fragment 를 떼고, 호스트를 소문자로 정규화한다."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    if not parts.scheme and not parts.netloc:
        return url.strip()

    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in _TRACKING_PARAMS]
    query.sort()
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def title_key(title: str) -> str:
    """제목 기반 유사 중복 판정용 키.

    소스별로 말머리/공백/특수문자가 달라서 그대로 비교하면 같은 딜을 놓친다.
    영숫자와 한글만 남겨 비교한다.
    """
    return _NON_WORD.sub("", title.lower())


def parse_price_krw(text: str) -> int | None:
    """제목 문자열에서 원화 가격을 최선 추정한다. 실패하면 None."""
    for pattern in _PRICE_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                value = int(match.group(1).replace(",", ""))
            except ValueError:
                continue
            # 4자리 미만은 "10원" 같은 노이즈, 1억 초과는 파싱 실패로 본다.
            if 100 <= value <= 100_000_000:
                return value
    return None


@dataclass(frozen=True, slots=True)
class Deal:
    """수집된 핫딜 하나."""

    source: str
    title: str
    url: str
    raw_id: str = ""
    posted_at: datetime | None = None
    price_krw: int | None = None
    shop: str | None = None
    category: str | None = None
    #: 가격 판정 결과 등 표시용 부가 정보. 중복제거 키에는 영향을 주지 않는다.
    note: str = field(default="", compare=False)
    extra: dict[str, str] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("Deal.title 은 비어 있을 수 없습니다")
        if not self.url.strip():
            raise ValueError("Deal.url 은 비어 있을 수 없습니다")

    #: 웹 링크가 없는 딜(토스 앱 카드 등)에 붙이는 접두사.
    PSEUDO_SCHEME = "app-deal://"

    @staticmethod
    def pseudo_url(namespace: str, title: str) -> str:
        """웹 URL 이 없는 딜에 안정적인 중복제거 키를 만들어준다.

        제목이 같으면 항상 같은 값이 나오므로, 화면을 다시 읽어도 같은 딜로
        인식된다. 포매터는 이 접두사를 보고 링크 대신 안내 문구를 낸다.
        """
        digest = hashlib.sha1(title.strip().encode("utf-8")).hexdigest()[:16]
        return f"{Deal.PSEUDO_SCHEME}{namespace}/{digest}"

    @property
    def has_web_link(self) -> bool:
        return not self.url.startswith(self.PSEUDO_SCHEME)

    @property
    def normalized_url(self) -> str:
        return normalize_url(self.url)

    @property
    def fingerprint(self) -> str:
        """URL 기준 1차 중복키."""
        basis = self.normalized_url or f"{self.source}:{self.raw_id}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()

    @property
    def title_key(self) -> str:
        """제목 기준 2차(교차 소스) 중복키."""
        return title_key(self.title)

    @property
    def age_seconds(self) -> float | None:
        if self.posted_at is None:
            return None
        posted = self.posted_at
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - posted).total_seconds()
