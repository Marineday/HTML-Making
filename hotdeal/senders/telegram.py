"""텔레그램 Bot API 전송기.

카카오와 달리 공식 API 이므로 계정 제재 위험이 없고, 앱 업데이트로 깨지지도
않는다. 코어 파이프라인을 검증할 때 이 경로부터 쓰는 것을 권한다.

준비:
  1) 텔레그램에서 @BotFather 에게 /newbot → 토큰 발급
  2) 봇을 채널/그룹에 초대하고 관리자 권한 부여
  3) chat_id 확인 (채널이면 "@채널이름" 도 가능)
"""

from __future__ import annotations

import json

from ..http import FetchError
from .base import SendError, Sender


class TelegramSender(Sender):
    style = "html"
    # 텔레그램은 도배 판정이 없으므로 딜 하나씩 보내 링크 미리보기를 살린다.
    default_batch_size = 1

    def validate(self) -> None:
        self.token = str(self.require("bot_token"))
        self.chat_id = str(self.require("chat_id"))
        self.disable_preview = bool(self.options.get("disable_web_page_preview", False))
        self.silent = bool(self.options.get("disable_notification", False))

    def send(self, text: str) -> None:
        payload = json.dumps(
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": self.disable_preview,
                "disable_notification": self.silent,
            },
            ensure_ascii=False,
        ).encode("utf-8")

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            raw = self.client.post_json(url, payload)
        except FetchError as exc:
            # 토큰이 예외 메시지에 섞여 나가지 않도록 URL 을 마스킹한다.
            raise SendError(f"텔레그램 전송 실패: {str(exc).replace(self.token, '***')}") from None

        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SendError(f"텔레그램 응답 파싱 실패: {exc}") from exc
        if not body.get("ok"):
            raise SendError(f"텔레그램 API 오류: {body.get('description', body)}")
