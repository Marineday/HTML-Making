"""제목에서 수량을 파싱해 '개당 단가'를 계산한다.

왜 필요한가
    "신라면 12,900원" 은 싼지 비싼지 알 수 없다. "20개입"이면 개당 645원이고
    "10개입"이면 1,290원이다. 총액만으로 딜을 평가하는 봇은 쓸모가 없다.

한계 (먼저 알고 쓸 것)
    제목 파싱은 원리상 100% 가 될 수 없다. 판매자가 제목을 자유롭게 쓴다.
    그래서 이 모듈은 확신이 없으면 None 을 반환한다 — 틀린 단가를 만들어
    "초특가"라고 외치는 것보다 침묵하는 편이 낫다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

# 수량 단위. 긴 것부터 매칭해야 "개입"이 "개"로 잘리지 않는다.
_COUNT_UNITS = (
    "개입", "개들이", "봉지", "캡슐", "세트", "묶음", "롤", "개", "입", "봉", "팩",
    "매", "장", "정", "캔", "병", "포", "알", "구", "통", "박스", "박", "ea", "pcs", "pc",
)
_VOLUME_UNITS = {"ml": 1.0, "mL": 1.0, "ML": 1.0, "cc": 1.0, "미리": 1.0,
                 "l": 1000.0, "L": 1000.0, "ℓ": 1000.0, "리터": 1000.0}
_WEIGHT_UNITS = {"g": 1.0, "G": 1.0, "그램": 1.0, "kg": 1000.0, "KG": 1000.0, "Kg": 1000.0, "킬로": 1000.0}

# 수량으로 오인하기 쉬운 것들을 미리 지운다.
_PRICE_TOKEN = re.compile(r"\d[\d,]*\s*원")
_PERCENT_TOKEN = re.compile(r"\d+\s*%")
_DATE_TOKEN = re.compile(r"\d{1,2}\s*[월일시분]")
_MULTIPLIER = re.compile(r"[x*×X]\s*(\d{1,4})(?![\d.])")

_COUNT_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(" + "|".join(re.escape(u) for u in _COUNT_UNITS) + r")(?![가-힣a-zA-Z])"
)
_VOLUME_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(리터|미리|ml|mL|ML|cc|ℓ|[lL])(?![가-힣a-zA-Z])"
)
_WEIGHT_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(킬로|그램|kg|KG|Kg|[gG])(?![가-힣a-zA-Z])"
)

# 사람이 실제로 사는 범위. 벗어나면 파싱 실패로 본다.
MAX_COUNT = 2000
MAX_VOLUME_ML = 500_000
MAX_WEIGHT_G = 500_000


class Dimension(str, Enum):
    """단가를 어떤 기준으로 매길지."""

    COUNT = "개"
    VOLUME = "100ml"
    WEIGHT = "100g"


@dataclass(frozen=True, slots=True)
class Quantity:
    """제목에서 추출한 수량. 모든 필드가 None 일 수 있다."""

    count: float | None = None
    volume_ml: float | None = None
    weight_g: float | None = None

    @property
    def dimension(self) -> Dimension | None:
        """단가 비교에 쓸 기준. 개수가 있으면 개수를 우선한다.

        같은 상품이라도 어떤 글은 '20개입', 어떤 글은 '2.4kg' 로 쓴다.
        기준이 섞이면 비교가 무의미하므로, 기록할 때 기준도 함께 저장하고
        같은 기준끼리만 비교한다.
        """
        if self.count:
            return Dimension.COUNT
        if self.volume_ml:
            return Dimension.VOLUME
        if self.weight_g:
            return Dimension.WEIGHT
        return None

    def units(self, dimension: Dimension) -> float | None:
        """해당 기준의 단위 수. 단가 = 가격 / units."""
        if dimension is Dimension.COUNT:
            return self.count
        if dimension is Dimension.VOLUME:
            return self.volume_ml / 100 if self.volume_ml else None
        if dimension is Dimension.WEIGHT:
            return self.weight_g / 100 if self.weight_g else None
        return None

    def describe(self) -> str:
        parts = []
        if self.count:
            parts.append(f"{_trim(self.count)}개")
        if self.volume_ml:
            parts.append(f"{_trim(self.volume_ml)}ml")
        if self.weight_g:
            parts.append(f"{_trim(self.weight_g)}g")
        return " / ".join(parts)


def _trim(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:g}"


def _strip_noise(title: str) -> str:
    """가격·할인율·날짜를 지운다. 안 지우면 수량으로 오인한다."""
    text = _PRICE_TOKEN.sub(" ", title)
    text = _PERCENT_TOKEN.sub(" ", text)
    return _DATE_TOKEN.sub(" ", text)


def parse_quantity(title: str) -> Quantity:
    """제목에서 수량을 추출한다. 못 찾으면 전부 None 인 Quantity."""
    text = _strip_noise(title)

    # "190ml*30캔" 에서 곱셈 표기(*30)와 단위 수량(30캔)은 같은 숫자다.
    # 숫자의 위치로 중복을 제거하지 않으면 30*30=900 이 되어버린다.
    factors_by_position: dict[int, float] = {}
    for match in _COUNT_PATTERN.finditer(text):
        factors_by_position[match.start(1)] = float(match.group(1))
    for match in _MULTIPLIER.finditer(text):
        factors_by_position.setdefault(match.start(1), float(match.group(1)))

    count: float | None = None
    factors = [value for value in factors_by_position.values() if 0 < value <= MAX_COUNT]
    if factors:
        product = 1.0
        for value in factors:
            product *= value
        count = product if 0 < product <= MAX_COUNT else None

    volume = _first_measure(_VOLUME_PATTERN, text, _VOLUME_UNITS, MAX_VOLUME_ML)
    weight = _first_measure(_WEIGHT_PATTERN, text, _WEIGHT_UNITS, MAX_WEIGHT_G)

    # "500ml 20개" 는 개당 500ml 라는 뜻이므로 총량은 곱해야 한다.
    if volume is not None and count:
        volume = volume * count if volume * count <= MAX_VOLUME_ML else None
    if weight is not None and count:
        weight = weight * count if weight * count <= MAX_WEIGHT_G else None

    return Quantity(count=count, volume_ml=volume, weight_g=weight)


def _first_measure(pattern: re.Pattern[str], text: str, table: dict[str, float], cap: float) -> float | None:
    for match in pattern.finditer(text):
        unit = match.group(2)
        factor = table.get(unit) or table.get(unit.lower())
        if factor is None:
            continue
        value = float(match.group(1)) * factor
        if 0 < value <= cap:
            return value
    return None


def unit_price(total_price: int, quantity: Quantity, dimension: Dimension | None = None) -> tuple[float, Dimension] | None:
    """단가와 그 기준을 반환한다. 계산할 수 없으면 None."""
    if total_price <= 0:
        return None
    dimension = dimension or quantity.dimension
    if dimension is None:
        return None
    units = quantity.units(dimension)
    if not units or units <= 0:
        return None
    return total_price / units, dimension


# ─────────────── 상품 동일성 판정 ───────────────

# 딜 제목에 항상 붙지만 상품과 무관한 말들. 이걸 안 지우면
# "특가"가 들어간 모든 글이 같은 상품으로 묶인다.
_NOISE_WORDS = frozenset(
    {
        "특가", "초특가", "핫딜", "최저가", "역대급", "역대", "무료배송", "무배", "배송비",
        "쿠폰", "할인", "카드할인", "즉시할인", "추가할인", "적립", "행사", "이벤트", "한정",
        "품절", "재입고", "오늘만", "마감", "떴다", "겟", "구매", "판매", "정보", "공유",
        "본사", "정품", "국내", "해외", "직구", "무료", "택배", "당일발송", "빠른배송",
        "토스", "토스페이", "페이", "결제", "시특가", "앱", "링크", "클릭",
        # 쇼핑몰 이름. 같은 상품이 몰마다 다른 상품으로 갈라지는 것을 막는다.
        "쿠팡", "지마켓", "g마켓", "gmarket", "옥션", "11번가", "11st", "네이버", "스마트스토어",
        "위메프", "티몬", "인터파크", "롯데온", "ssg", "쓱", "이마트", "홈플러스", "컬리",
        "알리", "알리익스프레스", "테무", "아마존", "쿠쿠", "무신사", "올리브영", "다이소",
    }
)

# 뽐뿌·루리웹 등은 제목 맨 앞 대괄호에 쇼핑몰명을 넣는다.
# 이걸 남겨두면 "[G마켓] 신라면"과 "[쿠팡] 신라면"이 다른 상품이 된다.
_LEADING_BRACKET = re.compile(r"^\s*(?:[\[(（<][^\])）>]{0,20}[\])）>]\s*)+")
_TOKEN_SPLIT = re.compile(r"[^0-9A-Za-z가-힣]+")
_BRACKETED = re.compile(r"[\[\]()（）{}<>]")
_UNIT_SUFFIX = re.compile(
    r"^\d+(?:\.\d+)?(?:" + "|".join(re.escape(u) for u in _COUNT_UNITS) + r"|ml|l|g|kg|cc)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ProductKey:
    """상품 동일성 판정용 토큰 집합.

    완벽할 수 없다. '농심 신라면'과 '신라면'은 부분집합 관계로 같게 보고,
    '신라면'과 '신라면블랙'은 다르게 본다. 오판이 나오면 운영하면서
    _NOISE_WORDS 에 단어를 추가해 조정한다.
    """

    tokens: tuple[str, ...]

    @property
    def key(self) -> str:
        return " ".join(self.tokens)

    @property
    def anchor(self) -> str:
        """가장 긴 토큰. 인덱스 조회의 1차 후보군을 좁히는 데 쓴다."""
        return self.tokens[0] if self.tokens else ""

    @property
    def usable(self) -> bool:
        # 토큰이 하나뿐이면 오매칭 위험이 크다.
        return len(self.tokens) >= 1 and len(self.anchor) >= 2

    def matches(self, other: "ProductKey") -> bool:
        """한쪽 토큰 집합이 다른 쪽에 포함되면 같은 상품으로 본다.

        "농심 신라면" ⊇ "신라면"  → 같은 상품
        "신라면" vs "신라면블랙"   → 토큰이 다르므로 다른 상품
        """
        if not self.usable or not other.usable:
            return False
        mine, theirs = set(self.tokens), set(other.tokens)
        return mine <= theirs or theirs <= mine


def product_key(title: str) -> ProductKey:
    text = _LEADING_BRACKET.sub(" ", _strip_noise(title))
    text = _BRACKETED.sub(" ", text)
    tokens = []
    for raw in _TOKEN_SPLIT.split(text):
        token = raw.strip().lower()
        if len(token) < 2:
            continue
        if token in _NOISE_WORDS:
            continue
        if token.isdigit():
            continue
        if _UNIT_SUFFIX.match(token):  # "20개입", "500ml"
            continue
        tokens.append(token)

    # 긴 토큰일수록 상품 고유성이 높다. 앵커로 쓰기 위해 길이순 정렬.
    tokens = sorted(set(tokens), key=lambda t: (-len(t), t))
    return ProductKey(tuple(tokens))
