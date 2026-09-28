"""수신자 목록(CSV) 로드와 전화번호 정규화.

CSV 머리글: name,phone,channel,link,active,memo
  * name, phone 만 필수. 나머지는 비워도 된다.
  * channel 이 비면 설정의 default_channel 을 쓴다.
  * link 가 있으면 그 사람에게만 이 링크를 보낸다 (없으면 설정의 공통 링크).
  * active 가 n/no/0/false/x 이면 건너뛴다 — 행을 지우지 않고 잠시 빼둘 때.

엑셀에서 "CSV UTF-8" 로 저장하면 BOM 이 붙는데, utf-8-sig 로 읽어 처리한다.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

CHANNELS = ("sms", "kakao", "telegram")
_MOBILE = re.compile(r"^01[016789]\d{7,8}$")
_FALSY = {"n", "no", "0", "false", "x", "off", "아니오", "중지"}


class RecipientError(ValueError):
    """목록 파일이 잘못됐을 때. 몇 번째 줄인지 메시지에 담는다."""


@dataclass(frozen=True, slots=True)
class Recipient:
    name: str
    phone: str  # 01012345678 형태로 정규화된 값
    channel: str
    link: str = ""
    memo: str = ""

    @property
    def masked_phone(self) -> str:
        """로그·미리보기용. 가운데 자리를 가린다."""
        return f"{self.phone[:3]}-****-{self.phone[-4:]}"


def normalize_phone(raw: str) -> str:
    """'010-1234-5678', '+82 10 1234 5678', '821012345678' → '01012345678'.

    한국 휴대폰 번호가 아니면 RecipientError. 텔레그램이 넘겨주는 연락처 번호
    (국가코드 포함, + 유무 제각각)도 이 함수로 맞춘다.
    """
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("82"):
        digits = "0" + digits[2:].lstrip("0")
    elif re.fullmatch(r"1[016789]\d{7,8}", digits):
        # 엑셀이 숫자로 읽어 앞자리 0 을 지운 경우 (010-1234-5678 → 1012345678)
        digits = "0" + digits
    if not _MOBILE.match(digits):
        raise RecipientError(f"휴대폰 번호 형식이 아닙니다: {raw!r}")
    return digits


def load(path: str | Path, default_channel: str = "sms") -> list[Recipient]:
    path = Path(path)
    if not path.exists():
        raise RecipientError(f"수신자 목록이 없습니다: {path}. recipients.example.csv 를 복사해서 만드세요.")

    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = {h.strip().lower() for h in (reader.fieldnames or [])}
        missing = {"name", "phone"} - headers
        if missing:
            raise RecipientError(f"{path} 머리글에 {sorted(missing)} 열이 필요합니다")

        recipients: list[Recipient] = []
        seen: dict[str, int] = {}
        # 머리글이 1번 줄이므로 데이터는 2번 줄부터
        for line_no, row in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            if not any(row.values()) or row.get("name", "").startswith("#"):
                continue
            if row.get("active", "").lower() in _FALSY:
                continue
            try:
                recipients.append(_parse_row(row, default_channel))
            except RecipientError as exc:
                raise RecipientError(f"{path}:{line_no}: {exc}") from None

            phone = recipients[-1].phone
            if phone in seen:
                raise RecipientError(f"{path}:{line_no}: {seen[phone]}번 줄과 번호가 겹칩니다 — 같은 달에 두 번 발송됩니다")
            seen[phone] = line_no

    return recipients


def _parse_row(row: dict[str, str], default_channel: str) -> Recipient:
    name = row.get("name", "")
    if not name:
        raise RecipientError("name 이 비었습니다")
    channel = (row.get("channel") or default_channel).lower()
    if channel not in CHANNELS:
        raise RecipientError(f"channel 은 {CHANNELS} 중 하나여야 합니다 (받은 값: {channel!r})")
    link = row.get("link", "")
    if link and not link.startswith(("https://", "http://")):
        raise RecipientError(f"link 는 http(s):// 로 시작해야 합니다: {link!r}")
    return Recipient(
        name=name,
        phone=normalize_phone(row.get("phone", "")),
        channel=channel,
        link=link,
        memo=row.get("memo", ""),
    )
