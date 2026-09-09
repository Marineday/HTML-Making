"""디스코드 웹훅 전송기.

설정할 게 웹훅 URL 하나뿐이라 가장 빨리 검증할 수 있는 경로다.
"""

from __future__ import annotations

import json

from ..http import FetchError
from .base import SendError, Sender


class DiscordSender(Sender):
    style = "markdown"
    # 디스코드는 메시지당 2000자 제한이 있으므로 과하게 묶지 않는다.
    default_batch_size = 3

    MAX_CHARS = 1900

    def validate(self) -> None:
        self.webhook_url = str(self.require("webhook_url"))
        if not self.webhook_url.startswith("https://"):
            raise SendError("discord webhook_url 은 https:// 로 시작해야 합니다")
        self.username = self.options.get("username") or "핫딜봇"

    def send(self, text: str) -> None:
        if len(text) > self.MAX_CHARS:
            text = text[: self.MAX_CHARS - 1] + "…"
        payload = json.dumps({"content": text, "username": self.username}, ensure_ascii=False).encode("utf-8")
        try:
            self.client.post_json(self.webhook_url, payload)
        except FetchError as exc:
            raise SendError(f"디스코드 전송 실패: {exc}") from None
