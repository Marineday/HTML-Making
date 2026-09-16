"""용량 산정 — "큰 용량 없이 IP 개수만 확보하면 되는가" 를 숫자로 확인한다.

이 프로젝트의 전제는 이렇다. 사용자는 한 달에 딱 한 번, 20분만 접속한다.
그러면 한 IP 를 20명이 공유해도 대역폭은 사실상 문제가 되지 않는다.
이 모듈은 그 직관이 맞는지 계산하고, **어디서 틀리는지** 를 같이 내놓는다.

두 가지 숫자를 함께 본다.

    독립 도착(포아송)  — 사용자들이 서로 무관하게 아무 때나 접속한다는 가정.
                         현실적인 평상시 수치다.
    최악(전원 동시)     — 한 IP 의 20명이 같은 순간에 붙는 경우.

평상시 수치만 보고 회선을 고르면 안 된다. "이번 달 영상 오늘까지 보세요"
같은 공지 한 번이면 독립 가정은 그대로 깨지고 최악 수치가 현실이 된다.
그래서 verdict() 는 최악 수치를 기준으로 경고한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# 해상도별 스트리밍 비트레이트 (Mbps). 자체 서비스 인코딩 설정에 맞춰 바꿔 쓸 것.
BITRATE_MBPS = {
    "480p": 1.5,
    "720p": 3.0,
    "1080p": 5.0,
    "4k": 16.0,
}

MINUTES_PER_DAY = 1440


class CapacityError(ValueError):
    """용량 입력값 오류."""


@dataclass(frozen=True)
class Workload:
    """한 IP 에 걸리는 부하의 정의.

    peak_concentration
        한 달 전체 시간 중 실제 접속이 몰리는 구간의 비율. 1.0 이면 24시간
        고르게 퍼진다는 뜻이고, 0.1 이면 한 달 시간의 10% 안에 모든 접속이
        몰린다는 뜻이다. 기본값 0.1 은 "평일 저녁에 몰린다" 정도의 보수적
        가정이다. 접속 마감일을 공지하는 운영이면 0.01 이하로 낮춰 볼 것.
    """

    users_per_ip: int = 20
    minutes_per_user_per_month: float = 20.0
    stream_mbps: float = BITRATE_MBPS["1080p"]
    peak_concentration: float = 0.10
    days_per_month: int = 30
    risk: float = 1e-3

    def __post_init__(self) -> None:
        if self.users_per_ip < 1:
            raise CapacityError("IP 당 인원은 1 이상이어야 한다.")
        if self.minutes_per_user_per_month <= 0:
            raise CapacityError("1인당 월 접속 시간은 0보다 커야 한다.")
        if self.stream_mbps <= 0:
            raise CapacityError("스트림 비트레이트는 0보다 커야 한다.")
        if not 0 < self.peak_concentration <= 1:
            raise CapacityError("peak_concentration 은 0 초과 1 이하여야 한다.")
        if self.days_per_month < 1:
            raise CapacityError("월 일수는 1 이상이어야 한다.")
        if not 0 < self.risk < 1:
            raise CapacityError("risk 는 0 과 1 사이여야 한다.")
        if self.minutes_per_user_per_month > self.days_per_month * MINUTES_PER_DAY:
            raise CapacityError("1인당 월 접속 시간이 한 달보다 길다.")

    @property
    def month_minutes(self) -> int:
        return self.days_per_month * MINUTES_PER_DAY

    @property
    def peak_window_minutes(self) -> float:
        return self.month_minutes * self.peak_concentration


@dataclass(frozen=True)
class IpCapacity:
    """출구 IP 하나가 감당해야 하는 양."""

    load_avg: float
    load_peak: float
    concurrency_typical: int
    concurrency_worst: int
    mbps_typical: float
    mbps_worst: float
    transfer_gb_month: float

    def table(self) -> list[tuple[str, str]]:
        return [
            ("월 평균 동시접속", f"{self.load_avg:.4f} 명"),
            ("피크창 평균 동시접속", f"{self.load_peak:.4f} 명"),
            ("평상시 동시접속 (상위 0.1%)", f"{self.concurrency_typical} 명"),
            ("최악 동시접속 (전원 동시)", f"{self.concurrency_worst} 명"),
            ("평상시 필요 대역", f"{self.mbps_typical:.1f} Mbps"),
            ("최악 필요 대역", f"{self.mbps_worst:.1f} Mbps"),
            ("월 전송량", f"{self.transfer_gb_month:.1f} GB"),
        ]


@dataclass(frozen=True)
class FleetCapacity:
    """전체 규모."""

    user_count: int
    ip_count: int
    per_ip: IpCapacity
    total_transfer_gb_month: float
    total_mbps_worst: float

    def table(self) -> list[tuple[str, str]]:
        return [
            ("사용자", f"{self.user_count:,} 명"),
            ("출구 IP", f"{self.ip_count:,} 개"),
            ("전체 월 전송량", f"{self.total_transfer_gb_month:,.0f} GB"),
            ("전체 최악 대역", f"{self.total_mbps_worst:,.0f} Mbps"),
        ]


def poisson_quantile(mean: float, risk: float) -> int:
    """P(X > k) <= risk 를 만족하는 가장 작은 k.

    평균이 아주 작으면 0 이 나온다 — 그게 정상이다. 월 20분짜리 부하는
    대부분의 순간에 아무도 접속해 있지 않다.
    """
    if mean <= 0:
        return 0
    if mean > 400:  # exp(-mean) 이 언더플로하는 구간. 정규근사로 충분하다.
        return int(math.ceil(mean + 3.1 * math.sqrt(mean)))
    target = 1.0 - risk
    term = math.exp(-mean)
    cum = term
    k = 0
    while cum < target and k < 100_000:
        k += 1
        term *= mean / k
        cum += term
    return k


def for_ip(load: Workload) -> IpCapacity:
    """IP 하나의 용량을 계산한다."""
    user_minutes = load.users_per_ip * load.minutes_per_user_per_month
    load_avg = user_minutes / load.month_minutes
    load_peak = min(float(load.users_per_ip), user_minutes / load.peak_window_minutes)

    typical = min(load.users_per_ip, poisson_quantile(load_peak, load.risk))
    # 동시접속이 0 으로 나와도 회선은 한 명은 받아야 한다.
    typical = max(1, typical)

    # 초 단위 * Mbps / 8 = Mb -> MB, /1000 = GB (십진, 회선 사업자 표기 기준)
    transfer_gb = user_minutes * 60.0 * load.stream_mbps / 8.0 / 1000.0

    return IpCapacity(
        load_avg=load_avg,
        load_peak=load_peak,
        concurrency_typical=typical,
        concurrency_worst=load.users_per_ip,
        mbps_typical=typical * load.stream_mbps,
        mbps_worst=load.users_per_ip * load.stream_mbps,
        transfer_gb_month=transfer_gb,
    )


def for_fleet(user_count: int, ip_count: int, load: Workload) -> FleetCapacity:
    if ip_count < 1:
        raise CapacityError("출구 IP 는 1개 이상이어야 한다.")
    per_ip = for_ip(load)
    return FleetCapacity(
        user_count=user_count,
        ip_count=ip_count,
        per_ip=per_ip,
        total_transfer_gb_month=per_ip.transfer_gb_month * ip_count,
        total_mbps_worst=per_ip.mbps_worst * ip_count,
    )


def verdict(cap: IpCapacity, *, link_mbps: float, transfer_quota_gb: float | None = None) -> list[str]:
    """IP 하나에 붙일 회선이 충분한지 판정한다. 빈 목록이면 여유가 있다.

    판정 기준은 최악 수치다. 평상시 수치로 회선을 고르면 공지 한 번에
    묶음 전체가 버퍼링을 맞는다.
    """
    problems: list[str] = []
    if link_mbps <= 0:
        raise CapacityError("회선 속도는 0보다 커야 한다.")
    if cap.mbps_worst > link_mbps:
        problems.append(f"전원 동시 접속 시 {cap.mbps_worst:.0f} Mbps 가 필요한데 회선은 {link_mbps:.0f} Mbps 다. " f"동시 {int(link_mbps // (cap.mbps_worst / cap.concurrency_worst))}명까지만 버틴다.")
    if transfer_quota_gb is not None:
        if transfer_quota_gb <= 0:
            raise CapacityError("전송량 한도는 0보다 커야 한다.")
        if cap.transfer_gb_month > transfer_quota_gb:
            problems.append(f"월 전송량 {cap.transfer_gb_month:.0f} GB 가 한도 {transfer_quota_gb:.0f} GB 를 넘는다.")
        elif cap.transfer_gb_month > transfer_quota_gb * 0.8:
            problems.append(f"월 전송량 {cap.transfer_gb_month:.0f} GB 가 한도 {transfer_quota_gb:.0f} GB 의 80% 를 넘는다. " "사용자가 예상보다 더 보면 초과 과금이 난다.")
    return problems


__all__ = [
    "BITRATE_MBPS",
    "CapacityError",
    "FleetCapacity",
    "IpCapacity",
    "Workload",
    "for_fleet",
    "for_ip",
    "poisson_quantile",
    "verdict",
]
