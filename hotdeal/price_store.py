"""가격 이력 저장과 'N일 평균 대비' 판정.

핵심 아이디어
    같은 상품의 과거 단가를 쌓아두고, 새 딜의 단가가 그 분포에서 어디쯤인지
    본다. 12,900원이 싼지는 알 수 없지만, "최근 30일 개당 중앙값 900원인데
    이번엔 645원" 이면 초특가라고 말할 수 있다.

왜 평균이 아니라 중앙값인가
    제목 파싱은 반드시 일부 틀린다. 오파싱 한 건이 평균을 통째로 망가뜨리지만
    중앙값은 거의 흔들리지 않는다. 표본이 적을 때는 아예 판정하지 않는다 —
    틀린 단가로 "초특가"를 외치는 것이 봇의 신뢰를 가장 빨리 죽인다.

상품 동일성
    토큰 부분집합으로 판정한다(pricing.ProductKey). 토큰 인덱스 테이블로
    후보를 좁힌 뒤 파이썬에서 검증한다.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from .models import Deal
from .pricing import Dimension, ProductKey, Quantity, parse_quantity, product_key, unit_price

_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    product_key TEXT NOT NULL,
    dimension   TEXT NOT NULL,
    unit_price  REAL NOT NULL,
    total_price INTEGER NOT NULL,
    title       TEXT NOT NULL,
    source      TEXT NOT NULL,
    url         TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_product ON price_history(product_key, dimension);
CREATE INDEX IF NOT EXISTS idx_price_observed ON price_history(observed_at);

CREATE TABLE IF NOT EXISTS price_tokens (
    token     TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    PRIMARY KEY (token, record_id)
);
CREATE INDEX IF NOT EXISTS idx_price_tokens_record ON price_tokens(record_id);
"""


class Grade(str, Enum):
    SUPER = "초특가"
    GOOD = "특가"
    BELOW_AVERAGE = "평균이하"
    NORMAL = "평범"
    INSUFFICIENT = "데이터부족"
    UNKNOWN = "단가불명"

    @property
    def is_notable(self) -> bool:
        return self in (Grade.SUPER, Grade.GOOD)

    @property
    def badge(self) -> str:
        return {
            Grade.SUPER: "🔥🔥🔥 초특가",
            Grade.GOOD: "🔥 특가",
            Grade.BELOW_AVERAGE: "🙂 평균 이하",
            Grade.NORMAL: "😐 평범",
            Grade.INSUFFICIENT: "🆕 비교 데이터 부족",
            Grade.UNKNOWN: "",
        }[self]


@dataclass(frozen=True, slots=True)
class PriceVerdict:
    """한 딜에 대한 가격 판정 결과."""

    grade: Grade
    quantity: Quantity
    unit_price: float | None = None
    dimension: Dimension | None = None
    baseline: float | None = None
    sample_size: int = 0
    window_days: int = 0
    all_time_low: float | None = None
    is_all_time_low: bool = False

    @property
    def discount_ratio(self) -> float | None:
        """기준가 대비 몇 % 싼지. 0.3 이면 30% 저렴."""
        if self.unit_price is None or not self.baseline:
            return None
        return 1.0 - (self.unit_price / self.baseline)

    def describe(self) -> str:
        """사람이 읽을 한 줄. 판정 근거를 반드시 같이 보여준다."""
        if self.grade is Grade.UNKNOWN:
            return ""
        if self.unit_price is None or self.dimension is None:
            return self.grade.badge

        unit = f"{self.unit_price:,.0f}원/{self.dimension.value}"
        if self.grade is Grade.INSUFFICIENT:
            return f"{self.grade.badge} · {unit} (표본 {self.sample_size}건)"

        pieces = [self.grade.badge, unit]
        if self.baseline:
            discount = self.discount_ratio or 0.0
            pieces.append(
                f"{self.window_days}일 중앙값 {self.baseline:,.0f}원 대비 {discount * 100:.0f}%↓"
                if discount >= 0
                else f"{self.window_days}일 중앙값 {self.baseline:,.0f}원 대비 {-discount * 100:.0f}%↑"
            )
        if self.is_all_time_low:
            pieces.append("역대 최저")
        pieces.append(f"표본 {self.sample_size}건")
        return " · ".join(pieces)


@dataclass(frozen=True, slots=True)
class PricingSettings:
    enabled: bool = True
    window_days: int = 30
    min_samples: int = 5
    super_deal_ratio: float = 0.70
    good_deal_ratio: float = 0.85
    below_average_ratio: float = 1.00
    retention_days: int = 180


class PriceStore:
    def __init__(self, path: str | Path, settings: PricingSettings | None = None) -> None:
        self.settings = settings or PricingSettings()
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---------- 기록 ----------

    def record(self, deal: Deal) -> bool:
        """딜의 단가를 이력에 남긴다. 이미 있거나 단가를 못 구하면 False.

        필터를 통과하지 못한 딜도 기록해야 한다 — 비교 기준을 만드는 것이
        목적이므로 토스 딜만 모아서는 아무 의미가 없다.
        """
        observation = self._observe(deal)
        if observation is None:
            return False
        key, dimension, price = observation

        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO price_history"
            " (fingerprint, product_key, dimension, unit_price, total_price, title, source, url, observed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                deal.fingerprint,
                key.key,
                dimension.value,
                price,
                deal.price_krw,
                deal.title,
                deal.source,
                deal.normalized_url,
                (deal.posted_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
            ),
        )
        if cursor.rowcount == 0:  # 이미 기록된 딜
            self._conn.commit()
            return False

        record_id = int(cursor.lastrowid or 0)
        self._conn.executemany(
            "INSERT OR IGNORE INTO price_tokens (token, record_id) VALUES (?, ?)",
            [(token, record_id) for token in key.tokens],
        )
        self._conn.commit()
        return True

    def record_many(self, deals: list[Deal]) -> int:
        return sum(1 for deal in deals if self.record(deal))

    # ---------- 판정 ----------

    def evaluate(self, deal: Deal) -> PriceVerdict:
        """딜의 가격 등급을 판정한다. 기록은 하지 않는다."""
        quantity = parse_quantity(deal.title)
        if not self.settings.enabled or deal.price_krw is None:
            return PriceVerdict(grade=Grade.UNKNOWN, quantity=quantity)

        computed = unit_price(deal.price_krw, quantity)
        if computed is None:
            return PriceVerdict(grade=Grade.UNKNOWN, quantity=quantity)
        price, dimension = computed

        key = product_key(deal.title)
        if not key.usable:
            return PriceVerdict(grade=Grade.UNKNOWN, quantity=quantity, unit_price=price, dimension=dimension)

        history = self._history_for(key, dimension, self.settings.window_days, exclude=deal.fingerprint)
        all_time = self._history_for(key, dimension, None, exclude=deal.fingerprint)
        all_time_low = min(all_time) if all_time else None

        base = PriceVerdict(
            grade=Grade.INSUFFICIENT,
            quantity=quantity,
            unit_price=price,
            dimension=dimension,
            sample_size=len(history),
            window_days=self.settings.window_days,
            all_time_low=all_time_low,
            is_all_time_low=bool(all_time_low is not None and price < all_time_low),
        )
        if len(history) < self.settings.min_samples:
            return base

        baseline = statistics.median(history)
        if baseline <= 0:
            return base

        ratio = price / baseline
        if ratio <= self.settings.super_deal_ratio:
            grade = Grade.SUPER
        elif ratio <= self.settings.good_deal_ratio:
            grade = Grade.GOOD
        elif ratio <= self.settings.below_average_ratio:
            grade = Grade.BELOW_AVERAGE
        else:
            grade = Grade.NORMAL

        return PriceVerdict(
            grade=grade,
            quantity=quantity,
            unit_price=price,
            dimension=dimension,
            baseline=baseline,
            sample_size=len(history),
            window_days=self.settings.window_days,
            all_time_low=all_time_low,
            is_all_time_low=bool(all_time_low is not None and price < all_time_low),
        )

    # ---------- 내부 ----------

    def _observe(self, deal: Deal) -> tuple[ProductKey, Dimension, float] | None:
        if deal.price_krw is None:
            return None
        quantity = parse_quantity(deal.title)
        computed = unit_price(deal.price_krw, quantity)
        if computed is None:
            return None
        key = product_key(deal.title)
        if not key.usable:
            return None
        return key, computed[1], computed[0]

    def _history_for(
        self, key: ProductKey, dimension: Dimension, window_days: int | None, *, exclude: str = ""
    ) -> list[float]:
        """같은 상품·같은 기준의 과거 단가 목록.

        토큰 인덱스로 후보를 좁힌 뒤(SQL), 부분집합 판정은 파이썬에서 한다.
        SQL 만으로 집합 포함 관계를 표현하기 어렵고, 후보 수가 적어서
        이 정도면 충분히 빠르다.
        """
        if not key.tokens:
            return []

        placeholders = ",".join("?" * len(key.tokens))
        params: list[object] = list(key.tokens)
        query = (
            "SELECT h.product_key, h.unit_price, h.fingerprint FROM price_history h"
            f" WHERE h.dimension = ? AND h.id IN (SELECT record_id FROM price_tokens WHERE token IN ({placeholders}))"
        )
        params = [dimension.value, *key.tokens]
        if window_days is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
            query += " AND h.observed_at >= ?"
            params.append(cutoff)

        prices: list[float] = []
        for row in self._conn.execute(query, params):
            if exclude and row["fingerprint"] == exclude:
                continue
            candidate = ProductKey(tuple(row["product_key"].split()))
            if key.matches(candidate):
                prices.append(float(row["unit_price"]))
        return prices

    # ---------- 유지보수 ----------

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0])

    def prune(self, retention_days: int | None = None) -> int:
        days = self.settings.retention_days if retention_days is None else retention_days
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        self._conn.execute(
            "DELETE FROM price_tokens WHERE record_id IN (SELECT id FROM price_history WHERE observed_at < ?)",
            (cutoff,),
        )
        cursor = self._conn.execute("DELETE FROM price_history WHERE observed_at < ?", (cutoff,))
        self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PriceStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
