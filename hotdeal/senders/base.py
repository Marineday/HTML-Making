from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any

from ..formatter import Style
from ..http import HttpClient
from ..models import Deal


class SendError(RuntimeError):
    """전송 실패. 파이프라인은 이걸 잡고 다른 어댑터를 계속 시도한다."""


@dataclass(slots=True)
class SendOutcome:
    """부분 성공을 표현한다.

    한 어댑터가 3개 묶음 중 2개까지 보내고 실패할 수 있다. 이때 보낸 것은
    보낸 것으로 기록해야 다음 주기에 중복 발송되지 않는다.
    """

    delivered: list["Deal"] = field(default_factory=list)
    messages: int = 0
    error: str | None = None


class Sender(abc.ABC):
    #: 이 경로가 이해하는 메시지 서식
    style: Style = "plain"
    #: 한 메시지에 묶을 딜 개수. 1 이면 딜마다 한 통.
    default_batch_size = 1

    def __init__(self, options: dict[str, Any], client: HttpClient) -> None:
        self.options = options
        self.client = client
        self.batch_size = int(options.get("batch_size", self.default_batch_size))
        if self.batch_size < 1:
            raise SendError("batch_size 는 1 이상이어야 합니다")
        self.validate()

    def validate(self) -> None:
        """필수 옵션 검증. 실행 시작 시점에 터지게 해서 런타임 401 을 피한다."""

    @property
    def name(self) -> str:
        return type(self).__name__

    @abc.abstractmethod
    def send(self, text: str) -> None:
        """완성된 메시지 한 통을 보낸다."""

    def require(self, key: str) -> Any:
        value = self.options.get(key)
        if value in (None, ""):
            raise SendError(f"{self.name} 설정에 '{key}' 가 필요합니다")
        return value

    def send_deals(self, deals: list[Deal], gap_seconds: float = 0.0) -> SendOutcome:
        """딜들을 batch_size 단위로 묶어 보낸다.

        묶어 보내는 이유: 카카오톡은 짧은 메시지를 연달아 쏘면 도배로 판정한다.
        묶음 사이에는 gap_seconds 만큼 쉰다.

        중간에 실패하면 거기서 멈추고, 그때까지 나간 딜만 delivered 에 담는다.
        """
        from ..formatter import format_batch

        outcome = SendOutcome()
        chunks = [deals[i : i + self.batch_size] for i in range(0, len(deals), self.batch_size)]
        for index, chunk in enumerate(chunks):
            try:
                self.send(format_batch(chunk, self.style))
            except SendError as exc:
                outcome.error = str(exc)
                break
            except Exception as exc:  # noqa: BLE001
                outcome.error = repr(exc)
                break
            outcome.delivered.extend(chunk)
            outcome.messages += 1
            if gap_seconds > 0 and index < len(chunks) - 1:
                time.sleep(gap_seconds)
        return outcome
