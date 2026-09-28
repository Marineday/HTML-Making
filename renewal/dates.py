"""날짜 계산. 월 단위 연장에서 말일 처리가 핵심이다."""

from __future__ import annotations

import calendar
from datetime import date


def add_months(start: date, months: int) -> date:
    """1월 31일 + 1개월 = 2월 28일(윤년 29일). 없는 날짜는 그달 말일로 당긴다."""
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def parse_date(text: str) -> date:
    """'2026-10-05', '2026.10.05', '2026/10/5' 를 받는다."""
    cleaned = text.strip().replace(".", "-").replace("/", "-")
    parts = cleaned.split("-")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"날짜 형식이 아닙니다: {text!r} (예: 2026-10-05)")
    return date(int(parts[0]), int(parts[1]), int(parts[2]))


def korean(day: date) -> str:
    return f"{day.month}월 {day.day}일"


def dday_label(expires: date, today: date) -> str:
    diff = (expires - today).days
    if diff > 0:
        return f"D-{diff}"
    if diff == 0:
        return "D-DAY"
    return f"만료 {-diff}일 지남"
