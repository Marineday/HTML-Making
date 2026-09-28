"""텔레그램 수신자의 전화번호 ↔ chat_id 연결.

텔레그램 봇은 전화번호만으로 먼저 말을 걸 수 없다. 그래서:
  1) 수신자에게 봇 링크(t.me/<봇이름>)를 한 번 보내고
  2) 수신자가 /start → [📱 내 번호 공유] 버튼을 누르면
  3) 명단에 있는 번호인지 확인한 뒤 chat_id 를 저장한다.

다른 사람의 연락처를 전달해서 연결을 가로채지 못하도록, 공유된 연락처의
user_id 가 보낸 사람 본인과 같을 때만 받아들인다.
"""

from __future__ import annotations

import logging
from typing import Any

from .channels import TELEGRAM_API, ChannelError, Transport, http_post_json, send_telegram
from .recipients import Recipient, RecipientError, normalize_phone
from .store import Store

log = logging.getLogger(__name__)

OFFSET_KEY = "telegram_update_offset"
SHARE_KEYBOARD = {
    "keyboard": [[{"text": "📱 내 번호 공유", "request_contact": True}]],
    "one_time_keyboard": True,
    "resize_keyboard": True,
}
REMOVE_KEYBOARD = {"remove_keyboard": True}


def handle_update(update: dict[str, Any], by_phone: dict[str, Recipient], store: Store) -> tuple[str, str, dict[str, Any] | None] | None:
    """업데이트 하나를 처리하고 (chat_id, 답장, reply_markup) 을 돌려준다. 답할 게 없으면 None."""
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    # 그룹에서 번호를 받으면 다른 사람에게 노출된다. 1:1 대화만 받는다.
    if chat.get("type") != "private":
        return None
    chat_id = str(chat.get("id", ""))
    sender_id = (message.get("from") or {}).get("id")

    contact = message.get("contact")
    if contact:
        if contact.get("user_id") != sender_id:
            return chat_id, "본인 번호만 등록할 수 있어요. 아래 버튼을 눌러 주세요.", SHARE_KEYBOARD
        try:
            phone = normalize_phone(str(contact.get("phone_number", "")))
        except RecipientError:
            return chat_id, "한국 휴대폰 번호만 등록할 수 있어요.", REMOVE_KEYBOARD
        recipient = by_phone.get(phone)
        if recipient is None:
            log.info("명단에 없는 번호의 연결 시도: chat_id=%s", chat_id)
            return chat_id, "명단에 없는 번호예요. 운영자에게 문의해 주세요.", REMOVE_KEYBOARD
        store.link_telegram(phone, chat_id)
        log.info("텔레그램 연결: %s %s", recipient.name, recipient.masked_phone)
        return chat_id, f"✅ {recipient.name}님 연결 완료! 매월 이 대화방으로 링크를 보내드릴게요.", REMOVE_KEYBOARD

    text = str(message.get("text", ""))
    if text.startswith("/start"):
        return chat_id, "명단 확인을 위해 아래 버튼으로 번호를 공유해 주세요.", SHARE_KEYBOARD
    return None


def poll_once(token: str, recipients: list[Recipient], store: Store, *, transport: Transport = http_post_json, wait_seconds: int = 10) -> int:
    """getUpdates 를 한 번 호출해 처리한다. 처리한 업데이트 수를 돌려준다."""
    offset = int(store.get_value(OFFSET_KEY, "0") or 0)
    try:
        body = transport(f"{TELEGRAM_API}/bot{token}/getUpdates", {"offset": offset, "timeout": wait_seconds, "allowed_updates": ["message"]}, {})
    except ChannelError as exc:
        raise ChannelError(str(exc).replace(token, "***")) from None
    if not body.get("ok"):
        raise ChannelError(f"텔레그램 getUpdates 오류: {body.get('description', body)}")

    by_phone = {r.phone: r for r in recipients}
    updates = body.get("result") or []
    for update in updates:
        reply = handle_update(update, by_phone, store)
        if reply is not None:
            chat_id, text, markup = reply
            try:
                send_telegram(token, chat_id, text, transport, markup)
            except ChannelError as exc:
                log.warning("답장 실패 chat_id=%s: %s", chat_id, exc)
        # 처리 후 곧바로 offset 을 올려야 재시작 시 같은 업데이트를 두 번 처리하지 않는다.
        store.set_value(OFFSET_KEY, str(int(update["update_id"]) + 1))
    return len(updates)
