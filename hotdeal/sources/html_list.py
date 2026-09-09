"""RSS 가 없는 사이트용 범용 링크 목록 스크래퍼.

사이트마다 파서를 새로 짜면 개편 한 번에 전부 깨진다. 대신 "게시글 링크의
href 정규식" 하나만 설정으로 받아서 앵커 텍스트를 제목으로 쓴다.
정확도는 전용 파서보다 낮지만, 깨졌을 때 고치는 비용이 설정 한 줄이다.

주의: HTML 스크래핑은 RSS 보다 훨씬 잘 깨진다. RSS 가 있으면 RSS 를 써라.
"""

from __future__ import annotations

import html
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from ..models import Deal, parse_price_krw
from .base import Source, SourceError

log = logging.getLogger(__name__)

_WHITESPACE = re.compile(r"\s+")


class _AnchorCollector(HTMLParser):
    """<a href> 와 그 안의 텍스트를 순서대로 모은다."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []
        self._depth = 0
        self._href = ""
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        if self._depth:
            # 중첩 <a> 는 비정상 마크업이다. 바깥쪽만 신뢰한다.
            self._depth += 1
            return
        href = dict(attrs).get("href") or ""
        self._depth = 1
        self._href = href
        self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._depth:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._depth:
            return
        self._depth -= 1
        if self._depth == 0:
            text = _WHITESPACE.sub(" ", "".join(self._buffer)).strip()
            if self._href and text:
                self.anchors.append((self._href, html.unescape(text)))
            self._href = ""
            self._buffer = []


class HtmlListSource(Source):
    def fetch(self) -> list[Deal]:
        try:
            body = self.client.get_text(self.config.url)
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"[{self.name}] 페이지 다운로드 실패: {exc}") from exc
        return self.parse(body)

    def parse(self, body: str) -> list[Deal]:
        try:
            pattern = re.compile(self.config.link_pattern)
        except re.error as exc:
            raise SourceError(f"[{self.name}] link_pattern 정규식 오류: {exc}") from exc

        parser = _AnchorCollector()
        parser.feed(body)
        base = self.config.base_url or self.config.url

        deals: list[Deal] = []
        seen_urls: set[str] = set()
        for href, text in parser.anchors:
            if not pattern.search(href):
                continue
            url = urljoin(base, href)
            if url in seen_urls:
                # 목록 페이지는 같은 글을 썸네일/제목 두 번 링크하는 경우가 흔하다.
                continue
            seen_urls.add(url)
            deals.append(
                Deal(
                    source=self.name,
                    title=text,
                    url=url,
                    raw_id=href,
                    # HTML 목록에서는 게시 시각을 신뢰성 있게 못 뽑는다.
                    # posted_at=None 이면 필터가 나이 검사를 건너뛴다.
                    posted_at=None,
                    price_krw=parse_price_krw(text),
                    category=self.config.category,
                )
            )
            if len(deals) >= self.config.max_items:
                break

        if not deals:
            log.warning("[%s] link_pattern %r 에 맞는 링크가 없습니다. 사이트 개편 가능성.", self.name, self.config.link_pattern)
        return deals
