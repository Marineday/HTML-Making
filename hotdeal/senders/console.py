"""표준출력 전송기. dry-run 과 소스 검증에 쓴다."""

from __future__ import annotations

from .base import Sender


class ConsoleSender(Sender):
    style = "plain"

    def send(self, text: str) -> None:
        print("-" * 48)
        print(text)
