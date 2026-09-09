"""표준 라이브러리만 쓰는 예의 바른 HTTP 클라이언트.

핫딜 커뮤니티는 대부분 개인이 운영하거나 트래픽에 민감하다. 크롤러가
공격적으로 때리면 차단당하고, 그건 봇이 죽는 가장 흔한 원인이다.
그래서 여기서 강제하는 것:

  * robots.txt 준수 (기본 on, 설정으로만 끌 수 있음)
  * 호스트별 최소 요청 간격
  * 지수 백오프 재시도
  * 정직한 User-Agent
"""

from __future__ import annotations

import gzip
import logging
import re
import threading
import time
import urllib.error
import urllib.request
import urllib.robotparser
import zlib
from dataclasses import dataclass, field
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "hotdeal-bot/0.1 (+personal use; contact via repository issues)"


class FetchError(RuntimeError):
    """재시도까지 소진한 뒤의 최종 실패."""


class RobotsDisallowed(FetchError):
    """robots.txt 가 해당 경로를 금지함."""


@dataclass(slots=True)
class HttpClient:
    user_agent: str = DEFAULT_USER_AGENT
    timeout: float = 15.0
    min_interval_per_host: float = 2.0
    max_retries: int = 3
    backoff_base: float = 2.0
    respect_robots: bool = True

    _lock: threading.Lock = field(init=False, repr=False, compare=False, default_factory=threading.Lock)
    _last_request_at: dict[str, float] = field(init=False, repr=False, compare=False, default_factory=dict)
    _robots: dict[str, object] = field(init=False, repr=False, compare=False, default_factory=dict)

    # ---------- 공개 API ----------

    def get_text(self, url: str) -> str:
        body, charset = self._get(url)
        return body.decode(charset, errors="replace")

    def get_bytes(self, url: str) -> bytes:
        body, _ = self._get(url)
        return body

    def post_json(self, url: str, payload: bytes, headers: dict[str, str] | None = None) -> bytes:
        """전송 어댑터용. 아웃바운드 API 호출이므로 robots 검사는 하지 않는다."""
        request = urllib.request.Request(url, data=payload, method="POST")
        request.add_header("User-Agent", self.user_agent)
        request.add_header("Content-Type", "application/json; charset=utf-8")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        return self._send_with_retry(request, url)

    # ---------- 내부 ----------

    def _get(self, url: str) -> tuple[bytes, str]:
        if self.respect_robots and not self._robots_allows(url):
            raise RobotsDisallowed(f"robots.txt 가 접근을 금지합니다: {url}")

        request = urllib.request.Request(url, method="GET")
        request.add_header("User-Agent", self.user_agent)
        request.add_header("Accept-Encoding", "gzip, deflate")
        request.add_header("Accept-Language", "ko-KR,ko;q=0.9,en;q=0.5")
        header_charset: list[str] = []
        body = self._send_with_retry(request, url, header_charset)
        return body, detect_charset(body, header_charset[0] if header_charset else "")

    def _send_with_retry(
        self, request: urllib.request.Request, url: str, charset_out: list[str] | None = None
    ) -> bytes:
        host = urlsplit(url).netloc
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            self._throttle(host)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    if charset_out is not None:
                        charset_out.append(response.headers.get_content_charset() or "")
                    return _decode_body(response.read(), response.headers.get("Content-Encoding", ""))
            except urllib.error.HTTPError as exc:
                last_error = exc
                # 4xx 는 재시도해도 같은 결과다. 단 429/408 은 예외.
                if exc.code not in (408, 429) and 400 <= exc.code < 500:
                    raise FetchError(f"{url} -> HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc

            if attempt < self.max_retries - 1:
                delay = self.backoff_base**attempt
                log.warning("fetch 실패 (%s/%s) %s: %s — %.1fs 후 재시도", attempt + 1, self.max_retries, url, last_error, delay)
                time.sleep(delay)

        raise FetchError(f"{url} 재시도 {self.max_retries}회 모두 실패: {last_error}") from last_error

    def _throttle(self, host: str) -> None:
        with self._lock:
            last = self._last_request_at.get(host)
            now = time.monotonic()
            if last is not None:
                wait = self.min_interval_per_host - (now - last)
                if wait > 0:
                    time.sleep(wait)
                    now = time.monotonic()
            self._last_request_at[host] = now

    def _robots_allows(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"

        with self._lock:
            cached = self._robots.get(origin, "miss")
        if cached == "miss":
            parser = self._load_robots(origin)
            with self._lock:
                self._robots[origin] = parser
        else:
            parser = cached

        # robots.txt 를 못 읽은 경우는 허용으로 본다(사이트 다수가 404 를 준다).
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    def _load_robots(self, origin: str) -> urllib.robotparser.RobotFileParser | None:
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(f"{origin}/robots.txt")
        try:
            request = urllib.request.Request(f"{origin}/robots.txt", method="GET")
            request.add_header("User-Agent", self.user_agent)
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                parser.parse(response.read().decode("utf-8", errors="replace").splitlines())
        except Exception as exc:  # noqa: BLE001 - robots 실패는 치명적이지 않다
            log.debug("robots.txt 로드 실패 %s: %s", origin, exc)
            return None
        return parser


_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["\']?\s*([A-Za-z0-9_\-]+)""",
    re.IGNORECASE,
)


def detect_charset(body: bytes, header_charset: str = "") -> str:
    """응답 인코딩을 판정한다.

    한국 커뮤니티 상당수(뽐뿌 등)가 아직 EUC-KR 을 쓴다. UTF-8 로 고정하면
    제목이 통째로 깨져서 필터도 중복제거도 전부 무의미해진다.

    우선순위: Content-Type 헤더 → HTML <meta charset> → UTF-8 디코딩 시도 → CP949.
    """
    for candidate in (header_charset, _sniff_meta_charset(body)):
        if candidate and _is_usable_codec(candidate):
            return _canonical(candidate)

    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        # UTF-8 로 안 읽히면 한국어 페이지에서는 사실상 CP949(EUC-KR 확장)다.
        return "cp949"
    return "utf-8"


def _sniff_meta_charset(body: bytes) -> str:
    # <meta> 는 문서 앞부분에 있다. 전체를 훑을 이유가 없다.
    match = _META_CHARSET.search(body[:4096])
    return match.group(1).decode("ascii", errors="ignore") if match else ""


def _canonical(name: str) -> str:
    # euc-kr 로 선언해 놓고 실제로는 확장 문자를 쓰는 페이지가 많다.
    # cp949 는 euc-kr 의 상위집합이라 더 안전하다.
    return "cp949" if name.lower().replace("_", "-") in ("euc-kr", "ks-c-5601-1987", "ksc5601") else name


def _is_usable_codec(name: str) -> bool:
    import codecs

    try:
        codecs.lookup(name)
    except LookupError:
        return False
    return True


def _decode_body(raw: bytes, content_encoding: str) -> bytes:
    encoding = content_encoding.lower()
    if "gzip" in encoding:
        return gzip.decompress(raw)
    if "deflate" in encoding:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw
