"""딜 → 메시지 문자열.

전송 경로마다 지원 문법이 다르다:
  * 카카오톡  : 서식 없음. 이모지와 줄바꿈만 쓸 수 있다.
  * 텔레그램  : HTML 파스모드
  * 디스코드  : 마크다운
"""

from __future__ import annotations

import html as html_mod
from typing import Literal

from .models import Deal

Style = Literal["plain", "html", "markdown"]

# 카카오톡 오픈채팅은 긴 메시지를 "메시지 전체보기" 로 접어버린다.
# 접히면 링크를 바로 못 누르므로 짧게 유지한다.
MAX_TITLE_CHARS = 90


def _clip(text: str, limit: int = MAX_TITLE_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _link_line(deal: Deal) -> str:
    """웹 링크가 없는 딜(토스 앱 카드)에 가짜 링크를 보여주면 안 된다."""
    return deal.url if deal.has_web_link else "토스 앱 → 홈 → 핫딜 에서 확인"


def _price_line(deal: Deal) -> str | None:
    if deal.price_krw is None:
        return None
    return f"💰 {deal.price_krw:,}원"


def format_deal(deal: Deal, style: Style = "plain", *, prefix: str = "🔥") -> str:
    title = _clip(deal.title)
    price = _price_line(deal)

    if style == "html":
        # 텔레그램 HTML 파스모드. 링크는 <a> 로 감싸지 않는다 —
        # 미리보기 카드가 뜨는 편이 핫딜에서는 더 유용하다.
        head = f"{prefix} <b>{html_mod.escape(title)}</b>"
        lines = [head, f"<i>{html_mod.escape(deal.source)}</i>"]
        if price:
            lines.append(html_mod.escape(price))
        if deal.note:
            lines.append(html_mod.escape(deal.note))
        lines.append(html_mod.escape(_link_line(deal)))
        return "\n".join(lines)

    if style == "markdown":
        lines = [f"{prefix} **{title}**", f"_{deal.source}_"]
        if price:
            lines.append(price)
        if deal.note:
            lines.append(deal.note)
        lines.append(_link_line(deal))
        return "\n".join(lines)

    lines = [f"{prefix} {title}", f"📍 {deal.source}"]
    if price:
        lines.append(price)
    if deal.note:
        lines.append(f"📊 {deal.note}")
    lines.append(f"🔗 {_link_line(deal)}")
    return "\n".join(lines)


def format_batch(deals: list[Deal], style: Style = "plain", *, separator: str = "\n\n") -> str:
    """여러 딜을 한 메시지로 묶는다.

    카카오톡에서 짧은 메시지를 연달아 쏘면 도배로 인식돼 제재 위험이 커진다.
    묶어 보내는 쪽이 안전하다.
    """
    return separator.join(format_deal(deal, style) for deal in deals)
