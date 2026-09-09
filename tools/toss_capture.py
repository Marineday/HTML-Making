#!/usr/bin/env python3
"""토스 앱 화면에서 핫딜을 읽어 브리지로 밀어넣는다.

무엇을 하는가 / 하지 않는가
    한다   : 안드로이드 접근성 트리(uiautomator 덤프)를 읽는다. 사람이 화면에서
             보는 것과 같은 정보다. 암호화·인증서 피닝과 아무 관련이 없다.
    안 한다: 앱 트래픽 가로채기(TLS MITM), 인증서 피닝 우회, 내부 API 호출.
             앱 업데이트마다 무너져서 봇으로 쓸 수 없다.

준비
    1) 기기에서 개발자 옵션 → USB 디버깅 켜기
    2) USB 연결 후  adb devices  로 인식 확인
       (무선으로 쓰려면  adb tcpip 5555 && adb connect <기기IP>:5555 )
    3) 기기 화면이 켜져 있고 잠금이 풀려 있어야 한다

사용
    # 화면을 덤프해서 파일로만 저장 (파서 조정용)
    python3 tools/toss_capture.py dump --out screen.xml

    # 덤프 파일을 파싱해 무엇이 잡히는지 확인 (기기 불필요)
    python3 tools/toss_capture.py parse screen.xml

    # 실제 수집 → 브리지로 전송
    python3 tools/toss_capture.py run --bridge http://127.0.0.1:8080 --source 토스핫딜

레이아웃이 바뀌어 아무것도 안 잡히면
    dump 로 XML 을 받아 parse 로 확인하면서 --min-texts / --price-only 를 조정하거나,
    이 파일의 heuristics 를 손보면 된다. 화면 구조가 바뀌는 것은 정상이고,
    그때 고쳐야 할 곳이 여기 한 군데로 모여 있다.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

DEVICE_DUMP_PATH = "/sdcard/window_dump.xml"

_PRICE = re.compile(r"([0-9][0-9,]{2,})\s*원")
_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# 카드 안에 섞여 있지만 상품명이 아닌 문구들.
_CHROME_WORDS = frozenset(
    {
        "핫딜", "오늘의 핫딜", "더보기", "전체보기", "구매하기", "바로가기", "닫기", "검색",
        "홈", "혜택", "전체", "주식", "송금", "내 자산", "쇼핑", "광고", "AD", "무료배송",
        "재입고", "품절", "마감임박", "찜하기", "공유",
    }
)


@dataclass(slots=True)
class UiNode:
    text: str
    bounds: tuple[int, int, int, int]
    clickable: bool
    children: list["UiNode"] = field(default_factory=list)

    @property
    def top(self) -> int:
        return self.bounds[1]

    @property
    def height(self) -> int:
        return self.bounds[3] - self.bounds[1]

    def texts(self) -> list[str]:
        """자신과 후손의 텍스트를 화면 순서대로."""
        found = [self.text] if self.text.strip() else []
        for child in sorted(self.children, key=lambda n: (n.bounds[1], n.bounds[0])):
            found.extend(child.texts())
        return found


@dataclass(slots=True)
class CapturedDeal:
    title: str
    price_krw: int | None
    url: str = ""

    def to_payload(self) -> dict:
        return {"title": self.title, "price_krw": self.price_krw, "url": self.url}


# ─────────────── XML 파싱 ───────────────


def parse_bounds(raw: str) -> tuple[int, int, int, int]:
    match = _BOUNDS.search(raw or "")
    if not match:
        return (0, 0, 0, 0)
    left, top, right, bottom = (int(value) for value in match.groups())
    return (left, top, right, bottom)


def build_tree(xml_bytes: bytes) -> UiNode:
    """uiautomator 덤프를 UiNode 트리로."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"UI 덤프 XML 파싱 실패: {exc}") from exc

    def convert(element: ET.Element) -> UiNode:
        node = UiNode(
            text=(element.get("text") or element.get("content-desc") or "").strip(),
            bounds=parse_bounds(element.get("bounds", "")),
            clickable=element.get("clickable") == "true",
        )
        node.children = [convert(child) for child in element if child.tag == "node"]
        return node

    nodes = [convert(child) for child in root if child.tag == "node"]
    if len(nodes) == 1:
        return nodes[0]
    return UiNode(text="", bounds=(0, 0, 0, 0), clickable=False, children=nodes)


def find_cards(root: UiNode, min_texts: int = 2) -> list[UiNode]:
    """딜 카드로 보이는 노드들.

    카드 = 텍스트를 min_texts 개 이상 품은 '가장 안쪽의' 클릭 가능한 노드.
    바깥쪽 컨테이너까지 카드로 잡으면 화면 전체가 하나의 딜이 되어버린다.
    """
    cards: list[UiNode] = []

    def walk(node: UiNode) -> bool:
        """이 서브트리에서 카드를 찾았으면 True."""
        found_below = False
        for child in node.children:
            if walk(child):
                found_below = True
        if found_below:
            return True
        if node.clickable and len([t for t in node.texts() if t.strip()]) >= min_texts:
            cards.append(node)
            return True
        return False

    walk(root)
    cards.sort(key=lambda n: (n.bounds[1], n.bounds[0]))
    return cards


def card_to_deal(card: UiNode) -> CapturedDeal | None:
    """카드의 텍스트들에서 상품명과 가격을 뽑는다."""
    texts = [t.strip() for t in card.texts() if t.strip()]
    if not texts:
        return None

    price: int | None = None
    for text in texts:
        match = _PRICE.search(text)
        if match:
            try:
                value = int(match.group(1).replace(",", ""))
            except ValueError:
                continue
            # 가장 낮은 금액을 판매가로 본다 (원가·할인전 가격이 같이 표시된다).
            if 100 <= value <= 100_000_000 and (price is None or value < price):
                price = value

    # 상품명 = 가격도 아니고 UI 문구도 아닌 것 중 가장 긴 텍스트.
    candidates = [
        text
        for text in texts
        if text not in _CHROME_WORDS and not _PRICE.fullmatch(text) and not text.replace("%", "").strip().isdigit()
    ]
    candidates = [text for text in candidates if len(text) >= 2]
    if not candidates:
        return None

    title = max(candidates, key=len)
    if _PRICE.search(title) and len(title) < 12:
        return None  # 가격 문구를 상품명으로 오인한 경우

    # 가격을 제목에 합쳐두면 다운스트림(단가 계산)이 그대로 쓸 수 있다.
    if price is not None and not _PRICE.search(title):
        title = f"{title} {price:,}원"

    return CapturedDeal(title=title[:300], price_krw=price)


def extract_deals(xml_bytes: bytes, min_texts: int = 2, price_only: bool = True) -> list[CapturedDeal]:
    root = build_tree(xml_bytes)
    deals: list[CapturedDeal] = []
    seen: set[str] = set()
    for card in find_cards(root, min_texts):
        deal = card_to_deal(card)
        if deal is None:
            continue
        if price_only and deal.price_krw is None:
            continue
        if deal.title in seen:
            continue
        seen.add(deal.title)
        deals.append(deal)
    return deals


# ─────────────── ADB ───────────────


class AdbError(RuntimeError):
    pass


def adb(args: list[str], serial: str = "", timeout: float = 30.0) -> bytes:
    command = ["adb"] + (["-s", serial] if serial else []) + args
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise AdbError("adb 를 찾을 수 없습니다. Android Platform Tools 를 설치하고 PATH 에 넣으세요.") from exc
    except subprocess.TimeoutExpired as exc:
        raise AdbError(f"adb 명령이 {timeout}초 안에 끝나지 않았습니다: {' '.join(command)}") from exc
    if result.returncode != 0:
        raise AdbError(f"adb 실패 ({result.returncode}): {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout


def dump_screen(serial: str = "") -> bytes:
    """현재 화면의 접근성 트리를 가져온다."""
    output = adb(["shell", "uiautomator", "dump", DEVICE_DUMP_PATH], serial).decode("utf-8", "replace")
    if "ERROR" in output.upper() and "dumped" not in output.lower():
        raise AdbError(f"uiautomator 덤프 실패: {output.strip()}\n화면이 켜져 있고 잠금이 풀렸는지 확인하세요.")
    return adb(["exec-out", "cat", DEVICE_DUMP_PATH], serial)


def open_toss(serial: str = "", package: str = "viva.republica.toss") -> None:
    adb(["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"], serial)


def scroll_down(serial: str = "") -> None:
    # 화면 중앙에서 위로 스와이프 = 아래로 스크롤
    adb(["shell", "input", "swipe", "540", "1600", "540", "700", "400"], serial)


# ─────────────── 브리지 전송 ───────────────


def post_to_bridge(bridge_url: str, token: str, source: str, deals: list[CapturedDeal]) -> dict:
    payload = json.dumps(
        {"source": source, "deals": [deal.to_payload() for deal in deals]}, ensure_ascii=False
    ).encode("utf-8")
    request = urllib.request.Request(f"{bridge_url.rstrip('/')}/ingest", data=payload, method="POST")
    request.add_header("Content-Type", "application/json; charset=utf-8")
    request.add_header("X-Bot-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"브리지 전송 실패 HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}") from None


# ─────────────── CLI ───────────────


def cmd_dump(args: argparse.Namespace) -> int:
    xml_bytes = dump_screen(args.serial)
    with open(args.out, "wb") as handle:
        handle.write(xml_bytes)
    print(f"{args.out} 에 저장했습니다 ({len(xml_bytes):,} bytes)")
    return 0


def cmd_parse(args: argparse.Namespace) -> int:
    with open(args.file, "rb") as handle:
        xml_bytes = handle.read()
    deals = extract_deals(xml_bytes, args.min_texts, not args.include_priceless)
    if not deals:
        print("잡힌 딜이 없습니다.")
        print("→ --min-texts 를 낮추거나 --include-priceless 로 확인해 보세요.")
        print("→ 그래도 없으면 화면이 핫딜 목록이 맞는지, 레이아웃이 바뀌지 않았는지 확인하세요.")
        return 1
    for deal in deals:
        price = f"{deal.price_krw:,}원" if deal.price_krw else "가격없음"
        print(f"  [{price:>12}] {deal.title}")
    print(f"\n총 {len(deals)}건")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if args.open_app:
        open_toss(args.serial)

    collected: dict[str, CapturedDeal] = {}
    for index in range(args.scrolls + 1):
        try:
            xml_bytes = dump_screen(args.serial)
        except AdbError as exc:
            print(f"덤프 실패: {exc}", file=sys.stderr)
            return 1
        for deal in extract_deals(xml_bytes, args.min_texts, not args.include_priceless):
            collected.setdefault(deal.title, deal)
        if index < args.scrolls:
            scroll_down(args.serial)

    deals = list(collected.values())
    print(f"{len(deals)}건 수집")
    for deal in deals:
        print(f"  [{(f'{deal.price_krw:,}원' if deal.price_krw else '가격없음'):>12}] {deal.title}")

    if not deals:
        return 1
    if args.dry_run:
        print("\n--dry-run 이므로 전송하지 않았습니다.")
        return 0

    result = post_to_bridge(args.bridge, args.token, args.source, deals)
    print(f"\n브리지 전송 완료: 신규 {result.get('accepted')} / 전체 {result.get('received')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="toss_capture.py", description="토스 앱 화면에서 핫딜 수집")
    parser.add_argument("--serial", default="", help="adb -s 로 넘길 기기 시리얼 (여러 대일 때)")
    parser.add_argument("--min-texts", type=int, default=2, help="카드로 인정할 최소 텍스트 수")
    parser.add_argument("--include-priceless", action="store_true", help="가격을 못 읽은 카드도 포함")
    sub = parser.add_subparsers(dest="command", required=True)

    dump = sub.add_parser("dump", help="현재 화면 UI 덤프를 파일로 저장")
    dump.add_argument("--out", default="screen.xml")
    dump.set_defaults(func=cmd_dump)

    parse = sub.add_parser("parse", help="저장한 덤프를 파싱해 결과 확인 (기기 불필요)")
    parse.add_argument("file")
    parse.set_defaults(func=cmd_parse)

    run = sub.add_parser("run", help="수집해서 브리지로 전송")
    run.add_argument("--bridge", default="http://127.0.0.1:8080")
    run.add_argument("--token", default="", help="비우면 환경변수 BRIDGE_TOKEN 사용")
    run.add_argument("--source", default="토스핫딜", help="소스 이름 (config.json 의 sources[].name 과 일치시킬 것)")
    run.add_argument("--scrolls", type=int, default=3, help="아래로 스크롤하며 몇 번 더 읽을지")
    run.add_argument("--open-app", action="store_true", help="시작 시 토스 앱을 실행")
    run.add_argument("--dry-run", action="store_true")
    run.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    import os

    args = build_parser().parse_args(argv)
    if getattr(args, "token", None) == "":
        args.token = os.environ.get("BRIDGE_TOKEN", "")
    if args.command == "run" and not args.token and not args.dry_run:
        print("BRIDGE_TOKEN 환경변수 또는 --token 이 필요합니다.", file=sys.stderr)
        return 2
    try:
        return int(args.func(args))
    except (AdbError, ValueError, RuntimeError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
