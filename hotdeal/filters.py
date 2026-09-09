"""딜 필터링.

한국어에는 단어 경계가 없어서 "토스" 로 필터링하면 "토스트기", "토스터"
같은 것이 전부 걸린다. 그래서 include 만으로는 부족하고 exclude 로
함정 단어를 같이 잡아줘야 한다 — config.example.json 에 기본값을 넣어뒀다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .config import FilterConfig
from .models import Deal

log = logging.getLogger(__name__)

_WHITESPACE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """대소문자와 공백 차이를 제거한다.

    "토스 페이" 와 "토스페이" 를 같게 보기 위해 공백을 아예 없앤다.
    """
    return _WHITESPACE.sub("", text.lower())


@dataclass(frozen=True, slots=True)
class FilterResult:
    passed: bool
    reason: str = ""


class DealFilter:
    def __init__(self, config: FilterConfig) -> None:
        self.config = config
        self._include = [_normalize(k) for k in config.include_keywords if k.strip()]
        self._exclude = [_normalize(k) for k in config.exclude_keywords if k.strip()]

    def check(self, deal: Deal) -> FilterResult:
        haystack = _normalize(f"{deal.title} {deal.shop or ''} {deal.category or ''}")

        for keyword in self._exclude:
            if keyword in haystack:
                return FilterResult(False, f"제외 키워드 '{keyword}'")

        if self._include:
            hits = [k for k in self._include if k in haystack]
            if self.config.require_all_keywords:
                if len(hits) != len(self._include):
                    missing = sorted(set(self._include) - set(hits))
                    return FilterResult(False, f"필수 키워드 누락 {missing}")
            elif not hits:
                return FilterResult(False, "포함 키워드 미일치")

        price = deal.price_krw
        if price is not None:
            if self.config.min_price_krw is not None and price < self.config.min_price_krw:
                return FilterResult(False, f"가격 {price} < 하한 {self.config.min_price_krw}")
            if self.config.max_price_krw is not None and price > self.config.max_price_krw:
                return FilterResult(False, f"가격 {price} > 상한 {self.config.max_price_krw}")

        if self.config.max_age_minutes is not None:
            age = deal.age_seconds
            # 게시 시각을 못 얻은 딜은 통과시킨다. 시간 정보가 없다고 버리면
            # RSS 에 pubDate 가 없는 소스가 통째로 죽는다.
            if age is not None and age > self.config.max_age_minutes * 60:
                return FilterResult(False, f"게시 후 {age / 60:.0f}분 경과 (상한 {self.config.max_age_minutes}분)")

        return FilterResult(True)

    def apply(self, deals: list[Deal]) -> list[Deal]:
        kept: list[Deal] = []
        for deal in deals:
            result = self.check(deal)
            if result.passed:
                kept.append(deal)
            else:
                log.debug("필터 탈락 [%s] %s — %s", deal.source, deal.title, result.reason)
        return kept
