from __future__ import annotations

import abc

from ..config import SourceConfig
from ..http import HttpClient
from ..models import Deal


class SourceError(RuntimeError):
    """소스 하나가 실패했을 때. 파이프라인은 이걸 잡고 나머지 소스를 계속 돈다."""


class Source(abc.ABC):
    def __init__(self, config: SourceConfig, client: HttpClient) -> None:
        self.config = config
        self.client = client

    @property
    def name(self) -> str:
        return self.config.name

    @abc.abstractmethod
    def fetch(self) -> list[Deal]:
        """딜 목록을 최신순으로 반환한다."""
