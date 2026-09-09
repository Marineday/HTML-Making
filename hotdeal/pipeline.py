"""수집 → 필터 → 중복제거 → 전송 파이프라인.

설계 원칙
  * 소스 하나가 죽어도 나머지는 계속 돈다. 커뮤니티 사이트는 자주 죽는다.
  * 전송 어댑터 하나가 죽어도 나머지는 계속 보낸다.
  * 전송에 전부 실패한 딜은 '봤음' 표시를 하지 않는다 → 다음 주기에 재시도.
  * 최초 실행 시에는 아무것도 보내지 않고 현재 피드를 전부 '봤음' 처리한다.
    이걸 안 하면 첫 실행에 밀린 피드 수백 건이 한꺼번에 방으로 쏟아져
    사실상 확정적으로 차단당한다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace

from . import senders as sender_registry
from . import sources as source_registry
from .config import Config
from .filters import DealFilter
from .http import DEFAULT_USER_AGENT, HttpClient
from .models import Deal
from .price_store import Grade, PriceStore, PricingSettings
from .senders.base import Sender
from .sources.base import SourceError
from .store import DealStore

log = logging.getLogger(__name__)


# min_grade 비교용 서열. 위가 더 좋은 딜이다.
_GRADE_RANK = {
    Grade.SUPER: 3,
    Grade.GOOD: 2,
    Grade.BELOW_AVERAGE: 1,
    Grade.NORMAL: 0,
}


@dataclass(slots=True)
class RunReport:
    collected: int = 0
    after_filter: int = 0
    new_deals: int = 0
    sent_messages: int = 0
    priced: int = 0
    graded: dict[str, int] = field(default_factory=dict)
    seeded: bool = False
    source_errors: dict[str, str] = field(default_factory=dict)
    sender_errors: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        parts = [
            f"수집 {self.collected}",
            f"필터통과 {self.after_filter}",
            f"신규 {self.new_deals}",
            f"발송 {self.sent_messages}통",
        ]
        if self.seeded:
            parts.append("(최초 실행: 발송 없이 기준선만 기록)")
        if self.priced:
            parts.append(f"가격기록 {self.priced}")
        if self.graded:
            parts.append("등급 " + ", ".join(f"{k} {v}" for k, v in sorted(self.graded.items())))
        if self.source_errors:
            parts.append(f"소스오류 {len(self.source_errors)}")
        if self.sender_errors:
            parts.append(f"전송오류 {len(self.sender_errors)}")
        return " | ".join(parts)


class Pipeline:
    def __init__(self, config: Config, *, store: DealStore | None = None, prices: PriceStore | None = None) -> None:
        self.config = config
        self.client = HttpClient(
            user_agent=config.user_agent or DEFAULT_USER_AGENT,
            respect_robots=config.respect_robots,
            min_interval_per_host=config.min_interval_per_host,
        )
        self.store = store or DealStore(config.db_path)
        self.prices = prices or PriceStore(
            config.pricing.db_path,
            PricingSettings(
                enabled=config.pricing.enabled,
                window_days=config.pricing.window_days,
                min_samples=config.pricing.min_samples,
                super_deal_ratio=config.pricing.super_deal_ratio,
                good_deal_ratio=config.pricing.good_deal_ratio,
                below_average_ratio=config.pricing.below_average_ratio,
                retention_days=config.pricing.retention_days,
            ),
        )
        self.filter = DealFilter(config.filters)
        self.sources = [source_registry.build(item, self.client) for item in config.enabled_sources]
        self.senders: list[Sender] = [sender_registry.build(item, self.client) for item in config.enabled_senders]
        if not self.sources:
            raise ValueError("활성화된 소스가 없습니다 (모든 sources 가 enabled=false)")
        if not self.senders:
            raise ValueError("활성화된 전송 어댑터가 없습니다 (모든 senders 가 enabled=false)")

    # ---------- 단계 ----------

    def collect(self, report: RunReport) -> list[Deal]:
        deals: list[Deal] = []
        for source in self.sources:
            try:
                fetched = source.fetch()
            except SourceError as exc:
                log.error("%s", exc)
                report.source_errors[source.name] = str(exc)
                continue
            except Exception as exc:  # noqa: BLE001 - 소스 하나 때문에 봇 전체가 죽으면 안 된다
                log.exception("[%s] 예상치 못한 수집 오류", source.name)
                report.source_errors[source.name] = repr(exc)
                continue
            log.info("[%s] %d건 수집", source.name, len(fetched))
            deals.extend(fetched)
        return deals

    def _grade(self, deals: list[Deal], report: RunReport) -> list[Deal]:
        """단가를 판정해 표시 문구를 붙이고, min_grade 미만은 걸러낸다."""
        if not self.config.pricing.enabled:
            return deals

        minimum = self.config.filters.min_grade
        allow_ungraded = self.config.filters.allow_ungraded
        kept: list[Deal] = []

        for deal in deals:
            verdict = self.prices.evaluate(deal)
            report.graded[verdict.grade.value] = report.graded.get(verdict.grade.value, 0) + 1

            if minimum:
                rank = _GRADE_RANK.get(verdict.grade)
                if rank is None:
                    # 등급을 매길 수 없는 딜 (단가 불명 / 표본 부족)
                    if not allow_ungraded:
                        log.debug("등급 미판정으로 제외: %s", deal.title)
                        continue
                elif rank < _GRADE_RANK[Grade(minimum)]:
                    log.debug("등급 미달(%s < %s)로 제외: %s", verdict.grade.value, minimum, deal.title)
                    continue

            note = verdict.describe()
            kept.append(replace(deal, note=note) if note else deal)

        return kept

    def _dispatch(self, deals: list[Deal], report: RunReport) -> list[Deal]:
        """모든 어댑터로 보내고, 최소 한 곳에는 전달된 딜만 돌려준다.

        어느 어댑터에도 못 나간 딜은 '봤음' 표시를 하지 않으므로 다음 주기에
        자동으로 재시도된다.
        """
        delivered: set[str] = set()
        for sender in self.senders:
            try:
                outcome = sender.send_deals(deals, self.config.send_gap_seconds)
            except Exception as exc:  # noqa: BLE001 - 어댑터 하나 때문에 봇이 죽으면 안 된다
                log.exception("[%s] 예상치 못한 전송 오류", sender.name)
                report.sender_errors[sender.name] = repr(exc)
                continue

            if outcome.error:
                log.error("[%s] %s", sender.name, outcome.error)
                report.sender_errors[sender.name] = outcome.error
            if outcome.messages:
                report.sent_messages += outcome.messages
                log.info("[%s] %d통 발송 (%d건)", sender.name, outcome.messages, len(outcome.delivered))
            delivered.update(deal.fingerprint for deal in outcome.delivered)

        return [deal for deal in deals if deal.fingerprint in delivered]

    # ---------- 실행 ----------

    def run_once(self, *, dry_run: bool = False, force_seed: bool = False) -> RunReport:
        report = RunReport()
        collected = self.collect(report)
        report.collected = len(collected)

        # 오래된 것부터 보내야 방에서 시간순으로 읽힌다.
        collected.sort(key=lambda d: (d.posted_at is None, d.posted_at))

        seeding = force_seed or (self.config.seed_on_first_run and self.store.count() == 0)
        if seeding:
            self.store.mark_many(collected)
            report.seeded = True
            log.warning(
                "최초 실행이므로 %d건을 발송 없이 기준선으로 기록했습니다. "
                "다음 주기부터 새로 올라오는 딜만 발송합니다.",
                len(collected),
            )
            return report

        # 가격 이력은 필터와 무관하게 전부 기록한다.
        # 토스 딜만 모아서는 "평소보다 싸다"를 판단할 기준이 생기지 않는다.
        if self.config.pricing.enabled and not dry_run:
            report.priced = self.prices.record_many(collected)

        passed: list[Deal] = []
        rejected: list[Deal] = []
        for deal in collected:
            (passed if self.filter.check(deal).passed else rejected).append(deal)
        report.after_filter = len(passed)

        # 탈락한 딜도 '봤음' 처리한다 — 매 주기 재평가할 이유가 없다.
        if not dry_run and rejected:
            self.store.mark_many(rejected)

        fresh = [deal for deal in passed if self.store.is_new(deal)]
        fresh = self._grade(fresh, report)
        report.new_deals = len(fresh)
        if not fresh:
            return report

        # 상한을 넘긴 분량은 이번에 안 보내고 '봤음' 표시도 하지 않는다.
        # 다음 주기에 자연스럽게 이어서 나간다.
        batch = fresh[: self.config.max_sends_per_run]
        if len(fresh) > len(batch):
            log.warning("신규 %d건 중 %d건만 발송합니다 (max_sends_per_run). 나머지는 다음 주기.", len(fresh), len(batch))

        if dry_run:
            for deal in batch:
                log.info("[dry-run] %s | %s", deal.title, deal.url)
            report.sent_messages = 0
            return report

        # 한 번에 넘겨서 각 어댑터가 자기 batch_size 대로 묶게 한다.
        # 도배 방지 간격(send_gap_seconds)은 어댑터가 묶음 사이에 적용한다.
        sent_ok = self._dispatch(batch, report)
        if sent_ok:
            self.store.mark_many(sent_ok)
        return report

    def run_forever(self, *, dry_run: bool = False) -> None:
        interval = self.config.poll_interval_seconds
        log.info("%d초 주기로 폴링을 시작합니다. Ctrl+C 로 종료.", interval)
        last_prune = 0.0
        while True:
            started = time.monotonic()
            try:
                report = self.run_once(dry_run=dry_run)
                log.info("주기 완료: %s", report.summary())
            except KeyboardInterrupt:
                raise
            except Exception:  # noqa: BLE001 - 루프는 어떤 예외에도 죽으면 안 된다
                log.exception("주기 실행 중 예외 발생. 다음 주기에 재시도합니다.")

            now = time.monotonic()
            if now - last_prune > 86400:
                removed = self.store.prune(self.config.dedup_retention_days)
                if removed:
                    log.info("오래된 중복제거 기록 %d건 정리", removed)
                removed = self.prices.prune()
                if removed:
                    log.info("오래된 가격 이력 %d건 정리", removed)
                last_prune = now

            elapsed = time.monotonic() - started
            time.sleep(max(1.0, interval - elapsed))

    def close(self) -> None:
        self.store.close()
        self.prices.close()
