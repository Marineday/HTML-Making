"""카카오 오픈채팅 전송기 (브리지 큐 경유).

⚠️ 경고 — 반드시 읽을 것
    카카오톡에는 오픈채팅방에 임의 메시지를 보내는 공식 API가 없다.
    이 경로는 안드로이드 기기에서 돌아가는 비공식 자동화 앱(메신저봇R 등)에
    의존하며, 이는 카카오 운영정책 위반 소지가 있다. 계정 영구정지 사례가
    실제로 있고, 방장 계정이 정지되면 오픈채팅방도 함께 잃는다.
    위험을 감수할 수 있을 때만 사용하라. 검증은 텔레그램/디스코드로 먼저 하라.

동작 구조
    이 전송기 ──POST /enqueue──> 브리지 서버(큐) <──GET /pull── 메신저봇R(안드로이드)
                                                                    │
                                                              카카오 오픈채팅방

    안드로이드 기기로 인바운드 연결을 여는 것은 (NAT/배터리 최적화 때문에)
    현실적이지 않다. 그래서 기기가 서버를 폴링해 가져가는 pull 방식을 쓴다.
"""

from __future__ import annotations

import json

from ..http import FetchError
from .base import SendError, Sender


class KakaoBridgeSender(Sender):
    style = "plain"
    # 카카오는 짧은 메시지를 연속으로 쏘면 도배로 인식된다. 묶어 보낸다.
    default_batch_size = 3

    def validate(self) -> None:
        self.bridge_url = str(self.require("bridge_url")).rstrip("/")
        self.room = str(self.require("room"))
        self.token = str(self.require("token"))
        if not self.bridge_url.startswith(("http://", "https://")):
            raise SendError("kakao_bridge 의 bridge_url 은 http:// 또는 https:// 로 시작해야 합니다")

    def send(self, text: str) -> None:
        payload = json.dumps({"room": self.room, "text": text}, ensure_ascii=False).encode("utf-8")
        try:
            raw = self.client.post_json(
                f"{self.bridge_url}/enqueue",
                payload,
                headers={"X-Bot-Token": self.token},
            )
        except FetchError as exc:
            raise SendError(f"브리지 큐 적재 실패: {str(exc).replace(self.token, '***')}") from None

        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SendError(f"브리지 응답 파싱 실패: {exc}") from exc
        if not body.get("ok"):
            raise SendError(f"브리지 오류: {body.get('error', body)}")
