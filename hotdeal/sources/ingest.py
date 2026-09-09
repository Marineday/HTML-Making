"""안드로이드 기기가 밀어넣은 딜을 읽어오는 소스.

토스 핫딜처럼 웹에서 접근할 수 없는 지면은 기기에서 수집해 브리지의
`POST /ingest` 로 밀어넣고, 이 소스가 `GET /inbox` 로 받아간다.

수집 방법(UI 덤프 / 알림 캡처)이 무엇이든 이 소스는 신경 쓰지 않는다.
새 수집 경로를 추가해도 여기는 그대로다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from ..models import Deal, parse_price_krw
from .base import Source, SourceError

log = logging.getLogger(__name__)


class IngestSource(Source):
    def fetch(self) -> list[Deal]:
        url = f"{self.config.url.rstrip('/')}/inbox?limit={self.config.max_items}"
        try:
            raw = self.client.get_bytes(url, {"X-Bot-Token": self.config.token})
        except Exception as exc:  # noqa: BLE001
            raise SourceError(f"[{self.name}] 인박스 조회 실패: {exc}") from exc
        return self.parse(raw)

    def parse(self, raw: bytes) -> list[Deal]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SourceError(f"[{self.name}] 인박스 응답 파싱 실패: {exc}") from exc
        if not payload.get("ok"):
            raise SourceError(f"[{self.name}] 브리지 오류: {payload.get('error', payload)}")

        deals: list[Deal] = []
        for item in payload.get("deals", [])[: self.config.max_items]:
            deal = self._to_deal(item)
            if deal is not None:
                deals.append(deal)
        return deals

    def _to_deal(self, item: dict) -> Deal | None:
        title = str(item.get("title") or "").strip()
        if not title:
            return None

        url = str(item.get("url") or "").strip()
        if not url:
            # 토스 앱 카드에는 웹 링크가 없다. 중복제거용 안정적인 의사 URL을
            # 만들고, 포매터가 이걸 알아보고 "앱에서 확인" 으로 표시한다.
            url = Deal.pseudo_url("toss", title)

        price = item.get("price_krw")
        try:
            price_krw = int(price) if price is not None else parse_price_krw(title)
        except (TypeError, ValueError):
            price_krw = parse_price_krw(title)

        return Deal(
            source=self.name,
            title=title,
            url=url,
            raw_id=url,
            # 기기가 방금 본 화면이므로 수집 시각을 게시 시각으로 본다.
            posted_at=datetime.now(timezone.utc),
            price_krw=price_krw,
            category=self.config.category,
        )
