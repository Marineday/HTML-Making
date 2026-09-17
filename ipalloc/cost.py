"""조달 방식별 월 비용 비교.

왜 필요한가
    "데이터센터 IP 는 비싸니 레지덴셜을 쓰자" 는 판단은 과금 단위를 보지 않은
    것이다. 데이터센터는 **IP 개수**로, 레지덴셜은 대부분 **트래픽 GB** 로
    과금한다. 어느 쪽이 싼지는 그 서비스의 IP 대 트래픽 비율이 결정한다.

    이 프로젝트는 IP 50개에 월 750 GB 다. IP 는 적고 트래픽은 그 몇 배다 —
    즉 IP 로 과금되는 쪽이 유리한 모양이다. 손익분기 GB 단가를 구하면
    그게 몇 배 차이인지 숫자로 나온다.

계산에 포함되지 않는 것
    레지덴셜 프록시는 HTTP/SOCKS 엔드포인트라 WireGuard 서버를 띄울 수 없고
    사용자가 인바운드로 붙을 수도 없다. 따라서 레지덴셜을 쓰더라도 **진입용
    데이터센터 호스트는 그대로 필요하다.** 아래 계산은 그 호스트 비용을
    양쪽 모두에 넣는다 — 레지덴셜은 대체가 아니라 추가이기 때문이다.

단가는 여기에 적지 않는다
    공급사·국가·시점에 따라 달라진다. 직접 받은 견적을 넣어야 의미가 있다.
"""

from __future__ import annotations

from dataclasses import dataclass


class CostError(ValueError):
    """비용 입력값 오류."""


@dataclass(frozen=True)
class PriceBook:
    """한 조달 방식의 단가표. 쓰지 않는 항목은 0 으로 둔다.

    ip_monthly
        고정 IP 1개의 월정액. 데이터센터 보조 IP, static ISP 프록시가 여기 해당.
    per_gb
        트래픽 GB 당 단가. 로테이팅 레지덴셜이 여기 해당.
    egress_per_gb
        호스트에서 나가는 전송량의 GB 당 단가. 클라우드는 붙고, 전송량이
        포함된 VPS 는 0 이다.
    """

    ip_monthly: float = 0.0
    host_monthly: float = 0.0
    hosts: int = 1
    egress_per_gb: float = 0.0
    per_gb: float = 0.0

    def __post_init__(self) -> None:
        for name in ("ip_monthly", "host_monthly", "egress_per_gb", "per_gb"):
            if getattr(self, name) < 0:
                raise CostError(f"{name} 는 0 이상이어야 한다.")
        if self.hosts < 0:
            raise CostError("호스트 수는 0 이상이어야 한다.")


@dataclass(frozen=True)
class Estimate:
    """한 조달 방식의 월 비용 내역."""

    label: str
    ip_cost: float
    host_cost: float
    traffic_cost: float

    @property
    def total(self) -> float:
        return self.ip_cost + self.host_cost + self.traffic_cost

    @property
    def fixed(self) -> float:
        """트래픽과 무관하게 나가는 비용."""
        return self.ip_cost + self.host_cost

    def table(self) -> list[tuple[str, str]]:
        return [
            ("IP", f"{self.ip_cost:,.2f}"),
            ("호스트", f"{self.host_cost:,.2f}"),
            ("트래픽", f"{self.traffic_cost:,.2f}"),
            ("합계", f"{self.total:,.2f}"),
        ]


def estimate(label: str, *, ip_count: int, transfer_gb: float, prices: PriceBook) -> Estimate:
    if ip_count < 0:
        raise CostError("IP 개수는 0 이상이어야 한다.")
    if transfer_gb < 0:
        raise CostError("전송량은 0 이상이어야 한다.")
    return Estimate(
        label=label,
        ip_cost=ip_count * prices.ip_monthly,
        host_cost=prices.hosts * prices.host_monthly,
        traffic_cost=transfer_gb * (prices.egress_per_gb + prices.per_gb),
    )


def breakeven_per_gb(target_total: float, fixed_cost: float, transfer_gb: float) -> float | None:
    """상대 방식과 같은 총액이 되는 GB 단가.

    이 값보다 싼 GB 단가를 받을 수 있어야 종량 방식이 이긴다. 음수가 나오면
    **고정비만으로 이미 상대를 넘는다** — GB 단가가 0원이어도 질 수 없는
    구조라는 뜻이고, 그때는 None 을 돌려준다.
    """
    if transfer_gb <= 0:
        raise CostError("전송량은 0보다 커야 한다.")
    margin = target_total - fixed_cost
    if margin <= 0:
        return None
    return margin / transfer_gb


@dataclass(frozen=True)
class ScaleRow:
    """IP 개수를 바꿔 봤을 때의 한 줄."""

    per_ip: int
    ip_count: int
    fixed_total: float
    metered_total: float

    @property
    def cheaper(self) -> str:
        if self.metered_total < self.fixed_total:
            return "종량"
        if self.fixed_total < self.metered_total:
            return "IP월정액"
        return "동일"


def scale(
    user_count: int,
    per_ip_values: list[int],
    *,
    transfer_gb: float,
    fixed_prices: PriceBook,
    metered_prices: PriceBook,
) -> list[ScaleRow]:
    """묶음 정원을 바꿔가며 두 과금 모델의 총액을 비교한다.

    여기서 드러나는 핵심은 이것이다. **전체 트래픽은 묶음 정원과 무관하다.**
    사용자 1인당 사용량이 정해져 있으면, 그 사람들을 몇 명씩 묶든 총 전송량은
    같다. 따라서

        종량 과금  → IP 개수와 무관하게 총액이 일정하다 (선형 증가하지 않는다)
        IP 월정액  → IP 개수에 정비례한다

    IP 를 많이 받아야 하는 설계라면 종량제가 구조적으로 유리하고, 적게 받아도
    되는 설계라면 월정액이 유리하다. 손익분기는 IP 개수에서 갈린다.
    """
    if user_count < 1:
        raise CostError("사용자 수는 1 이상이어야 한다.")
    if not per_ip_values:
        raise CostError("비교할 묶음 정원을 하나 이상 지정해야 한다.")

    rows: list[ScaleRow] = []
    for per_ip in sorted(set(per_ip_values)):
        if per_ip < 1:
            raise CostError("묶음 정원은 1 이상이어야 한다.")
        ip_count = -(-user_count // per_ip)
        rows.append(
            ScaleRow(
                per_ip=per_ip,
                ip_count=ip_count,
                fixed_total=estimate("f", ip_count=ip_count, transfer_gb=transfer_gb, prices=fixed_prices).total,
                metered_total=estimate("m", ip_count=ip_count, transfer_gb=transfer_gb, prices=metered_prices).total,
            )
        )
    return rows


def crossover_ip_count(fixed_prices: PriceBook, metered_prices: PriceBook, *, transfer_gb: float) -> int | None:
    """종량제가 월정액보다 싸지기 시작하는 IP 개수.

    월정액 총액 = ip_count * ip_monthly + 고정비 + 전송량비
    종량 총액   = ip_count * res_ip_monthly + 고정비 + 전송량비  (보통 res_ip_monthly = 0)

    두 식을 같게 놓고 ip_count 를 구한다. IP 단가 차이가 없으면 교차점이
    없으므로 None 을 돌려준다.
    """
    slope = fixed_prices.ip_monthly - metered_prices.ip_monthly
    if slope <= 0:
        return None
    fixed_side = fixed_prices.hosts * fixed_prices.host_monthly + transfer_gb * (fixed_prices.egress_per_gb + fixed_prices.per_gb)
    metered_side = metered_prices.hosts * metered_prices.host_monthly + transfer_gb * (metered_prices.egress_per_gb + metered_prices.per_gb)
    gap = metered_side - fixed_side
    if gap <= 0:
        return 1  # 고정비까지 종량제가 싸다 — IP 1개부터 이긴다
    return int(gap // slope) + 1


def compare(baseline: Estimate, candidate: Estimate) -> list[str]:
    """두 견적을 비교해 사람이 읽는 판정을 만든다."""
    out: list[str] = []
    if baseline.total <= 0:
        return ["단가를 넣지 않아 비교할 수 없다."]
    ratio = candidate.total / baseline.total
    # 라벨 뒤에 조사를 붙이면 한국어가 어색해진다. 콜론으로 끊는다.
    if candidate.total < baseline.total:
        out.append(f"{candidate.label}: {baseline.label} 대비 월 {baseline.total - candidate.total:,.2f} 저렴 ({ratio:.2f}배).")
    elif candidate.total > baseline.total:
        out.append(f"{candidate.label}: {baseline.label} 대비 월 {candidate.total - baseline.total:,.2f} 초과 ({ratio:.1f}배).")
    else:
        out.append(f"{candidate.label}: {baseline.label} 과 월 비용이 같다.")
    return out


__all__ = [
    "CostError",
    "Estimate",
    "PriceBook",
    "ScaleRow",
    "breakeven_per_gb",
    "compare",
    "crossover_ip_count",
    "estimate",
    "scale",
]
