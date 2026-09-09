"""범용 RSS 2.0 / Atom 수집기.

핫딜 커뮤니티 대부분이 RSS 를 열어두고 있으므로 사이트별 코드를 새로 짤
필요 없이 설정에 URL 만 추가하면 된다. 사이트가 개편돼도 RSS 구조는 잘
안 바뀌므로 HTML 스크래핑보다 훨씬 오래 버틴다.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from ..models import Deal, parse_price_krw
from .base import Source, SourceError

log = logging.getLogger(__name__)

_ATOM = "{http://www.w3.org/2005/Atom}"
_TAG_STRIP = re.compile(r"<[^>]+>")


def _text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return _TAG_STRIP.sub("", "".join(element.itertext())).strip()


def _parse_date(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    # RSS 는 RFC 822, Atom 은 ISO 8601 을 쓴다. 둘 다 시도한다.
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            log.debug("날짜 파싱 실패: %r", value)
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class RssSource(Source):
    def fetch(self) -> list[Deal]:
        try:
            body = self.client.get_bytes(self.config.url)
        except Exception as exc:  # noqa: BLE001 - 소스 단위 실패는 격리한다
            raise SourceError(f"[{self.name}] 피드 다운로드 실패: {exc}") from exc

        return self.parse(body)

    def parse(self, body: bytes) -> list[Deal]:
        """네트워크와 분리해 둔다 — 테스트에서 바이트를 직접 넣을 수 있다."""
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise SourceError(f"[{self.name}] XML 파싱 실패: {exc}") from exc

        entries = root.findall(".//item") or root.findall(f".//{_ATOM}entry")
        if not entries:
            log.warning("[%s] 피드에 항목이 없습니다. URL 이 RSS 가 맞는지 확인하세요: %s", self.name, self.config.url)
            return []

        deals: list[Deal] = []
        for entry in entries[: self.config.max_items]:
            deal = self._to_deal(entry)
            if deal is not None:
                deals.append(deal)
        return deals

    def _to_deal(self, entry: ET.Element) -> Deal | None:
        title = _text(entry.find("title")) or _text(entry.find(f"{_ATOM}title"))
        url = _text(entry.find("link")) or _text(entry.find(f"{_ATOM}link"))
        if not url:
            link = entry.find(f"{_ATOM}link")
            if link is not None:
                url = (link.get("href") or "").strip()
        if not title or not url:
            log.debug("[%s] title/link 없는 항목 건너뜀", self.name)
            return None

        raw_id = _text(entry.find("guid")) or _text(entry.find(f"{_ATOM}id")) or url
        published = (
            _text(entry.find("pubDate"))
            or _text(entry.find(f"{_ATOM}published"))
            or _text(entry.find(f"{_ATOM}updated"))
        )
        description = _text(entry.find("description")) or _text(entry.find(f"{_ATOM}summary"))

        return Deal(
            source=self.name,
            title=title,
            url=url,
            raw_id=raw_id,
            posted_at=_parse_date(published),
            # 가격은 제목에 있는 경우가 많고, 없으면 본문에서 한 번 더 찾는다.
            price_krw=parse_price_krw(title) or parse_price_krw(description),
            category=self.config.category,
        )
